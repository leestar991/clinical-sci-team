#!/usr/bin/env python3
"""确定性"无法判断"反查兜底闸（O1）。

背景：ECOG 检索失败根因是"证据在 OCR 文本里、但被埋在超长密集段落中，
LLM 注意力滑过"。纯自然语言提示（原则四/五/七）依赖模型注意力，对这类
attention 失败无效。本脚本提供**不依赖模型注意力的机械关键词检索**：

对每一条判定为「无法判断」且 `可从病例获取=true` 的条件，用该条件的
同义词 / 匹配字段 / 内置量表同义词在该患者的 OCR 汇总文本里做大小写不敏感
grep。命中即说明证据其实存在 —— 该「无法判断」是漏判（O2 中 QC 据此标阻断级）。

用法：
    python3 uncertain_recheck.py \
        --criteria /mnt/user-data/workspace/criteria_parsed.json \
        --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft.json \
        --ocr /mnt/user-data/workspace/patients/{id}/ocr/筛选期病历/ocr_records.md \
              /mnt/user-data/workspace/patients/{id}/ocr/筛选期检查/ocr_records.md \
        --out /mnt/user-data/workspace/patients/{id}/uncertain_recheck.json

产物 uncertain_recheck.json 结构：
    {
      "patient_id": "S042002",
      "checked": 12,                     // 参与反查的「无法判断」条目数
      "judgments_seen": 64,              // 判定文件里能读到的判定条目总数（不论结论）
      "unreadable_judgments": false,     // true = 一条都读不到 → 结构有问题，本闸结论不成立
      "suspected_missed": ["IN-8"],
      "entries": [
        {"条件ID": "IN-8", "document": "medical_record", "keywords": ["ECOG", ...],
         "hit": true, "grep_hits": [{"source": "...", "line_no": 42, "text": "...ECOG：1分..."}]}
      ]
    }

设计约束：
- 只检索 OCR 汇总文本（`ocr_records.md`），**绝不**检索 uploads/原始 PDF（与 fix-plan C1 一致）。
- 正常路径始终 exit 0；是否漏判以 JSON 的 `suspected_missed` 为准，便于子代理/QC 机械核验。
- **例外：`unreadable_judgments=true` 时 exit 2**。`checked == 0` 有两种截然不同的成因——本轨
  确实没有「无法判断」条目（正常），或判定文件结构读不出来（严重）。会话 `9a83ccc9` 里判定
  子代理把顶层 `judgments` 写成列表（自创 schema），本闸读到 0 条却打印「反查通过」，
  子代理据此回报 "gates pass"；而唯一能识破该文件的结构闸恰好被委派 prompt 漏掉了。
  「一条判定都读不到」必须自己出声，不能假定结构闸一定被跑过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 内置高频量表/指标同义词对照 —— 即便 criteria-parser 未填 `同义词`，
# 也能对这些"标准写全称、病历写缩写"的高危条件提供确定性关键词。
# 键为规范化后的匹配字段子串（小写），值为等价表述列表。
BUILTIN_SCALE_SYNONYMS: dict[str, list[str]] = {
    "ecog": ["ECOG", "ECOG评分", "体力状况", "体力状态", "一般状况", "一般情况", "PS评分", "performance status"],
    "kps": ["KPS", "Karnofsky", "卡氏评分", "卡诺夫斯基"],
    "tnm": ["TNM", "分期", "cT", "cN", "cM", "临床分期", "病理分期"],
    "crcl": ["CrCl", "肌酐清除率", "Ccr", "eGFR", "肾小球滤过率"],
    "g-csf": ["G-CSF", "粒细胞集落刺激因子", "重组人粒细胞", "非格司亭", "瑞白"],
    "hbsag": ["HBsAg", "乙肝表面抗原", "乙型肝炎表面抗原"],
    "anc": ["ANC", "中性粒细胞", "中性粒细胞绝对计数"],
    "egfr": ["EGFR", "表皮生长因子受体"],
    "kras": ["KRAS", "K-ras"],
    "msi": ["MSI", "微卫星不稳定", "MSI-H", "MSS", "错配修复", "dMMR", "pMMR"],
    # 「标准写类别、病历写药名」的类别项。与 `check_reason_alignment.py` 的 `_CLASS_TO_DRUGS`
    # 及 `criteria-parser/SKILL.md` 的内置同义词对照表**三处必须一致**（有一处漏就是盲区）。
    # 会话 `1fee1395`：EX-1-3 的 reason 称「无全身性糖皮质激素处方」，OCR 明写「地塞米松」。
    "糖皮质激素": [
        "糖皮质激素", "皮质激素", "皮质类固醇", "激素治疗",
        "地塞米松", "泼尼松", "泼尼松龙", "甲泼尼龙", "氢化可的松", "倍他米松", "曲安西龙", "可的松",
    ],
    "紫杉": ["紫杉类", "紫衫类", "多西他赛", "紫杉醇", "白蛋白紫杉醇", "卡巴他赛", "多帕菲"],
    "内分泌治疗": ["新型内分泌治疗", "阿比特龙", "恩扎卢胺", "氘恩扎如胺", "阿帕他胺", "达罗他胺", "瑞维鲁胺"],
}

UNCERTAIN_LABEL = "无法判断"
SUSPICIOUS_LABEL = "存疑"

# ── 轮次账本与分级熔断（Task 7）────────────────────────────────────────────
#
# 本闸原本无跨轮次状态，于是「跑闸 → 改产物 → 再跑闸」可以无限重复：会话 `2d628340` 跑了
# 12 次、`d393714d` 跑了 8 次，agent 在「改 reason 绕闸」上烧掉了判定阶段的大半 token。
# 范式直接借 `criteria-parser/scripts/check_track_structure.py` 的闸 8（QC 原地打转探测）：
# 每轮把结果写进账本，**同一集合连续 N 轮完全相同**即判定「修订无效」。
#
# ⛔ 分级不可混用（否则熔断会变成漏判的合法出口）：
#   - 阻断级 `suspected_missed`（结论「无法判断」但 OCR 里确有记录 → 疑似漏判）触顶
#     → `exit 3` + `stuck_items`，要求**失败上报**由 lead 依证据决定定向补跑；
#   - 建议级 `uncertain_hits`（结论「存疑」命中，只需据实改写 reason）触顶
#     → `exit 0` + 降级指令（标 `存疑` + `gate_escalated=true` 推进，交 QC/人工复核）。
ESCALATION_ROUNDS = 3
MAX_HISTORY_ROUNDS = 20

BLOCKING_ESCALATION_NOTE = (
    "⛔ 同一批 `suspected_missed` 已连续 {rounds} 轮未清（{items}）——这说明继续改产物不会让它通过。"
    "**禁止**再改写 reason/conclusion 去绕闸。按 `references/judge-delegation.md`「判定 task 失败后的处置」"
    "上报失败：列出 stuck_items、已尝试的动作、以及各条目当前的命中行，由主代理决定定向补跑或转人工复核。"
)

ADVISORY_ESCALATION_NOTE = (
    "ℹ️ 同一批 `uncertain_hits` 已连续 {rounds} 轮未清（{items}）。这是**建议级**：保留结论 `存疑`、"
    "标 `gate_escalated=true` 后**继续推进**，并把这些条目写入 QC 核验清单交人工复核。"
    "⛔ 不得为了清空本项而把 `存疑` 改成 `不符合`——那是错误排除，比漏判更贵。"
)

# lab 报告的**参考范围**列，不是"该患者有这件事"的记录。会话 `2d628340` §3.2：`男≤26`、
# `男 0-7`、`男 6-17` 全部被当成性别相关入排命中，agent 花了 6 个 grep + 6 个空 AI 步自证误报。
#
# ⚠️ 判据必须窄。第一版用了「任意 `数值-数值`」，结果把 `知情同意书签署=2026-04-15` 与
# `2025-03 起口服阿比特龙` 一起滤掉了——**日期长得就像区间**。召回下降比误报贵得多，所以这里
# 只认两种显式形态：① 行内出现「参考值/范围/区间」；② 性别紧跟比较符或数值区间（`男≤26`、
# `女 0-7`），也就是实测误报的那一种。
_REFERENCE_RANGE_PATTERNS = (
    re.compile(r"参考\s*(值|范围|区间)"),
    re.compile(r"(男|女)性?\s*[:：]?\s*([≤≥<>]\s*\d|\d+(\.\d+)?\s*[-–~—]\s*\d+)"),
)

# 宽泛的类别短语：命中的往往是标准原文的复述段而非患者用药记录。具体药名不在此列 ——
# `阿比特龙` 命中就是真实用药，必须继续报。
_BROAD_CLASS_PHRASES = frozenset(
    {
        "新型内分泌治疗",
        "内分泌治疗",
        "激素治疗",
        "糖皮质激素",
        "皮质激素",
        "皮质类固醇",
        "紫杉类",
        "紫衫类",
        "生物制剂",
        "免疫治疗",
        "靶向治疗",
        "全身治疗",
        "抗肿瘤治疗",
    }
)


def _looks_like_reference_range(line: str) -> bool:
    """命中行是否是化验单的参考范围列。"""
    return any(pattern.search(line) for pattern in _REFERENCE_RANGE_PATTERNS)


def is_broad_class_phrase(keyword: str) -> bool:
    return keyword.strip() in _BROAD_CLASS_PHRASES

# 两个结论都是「对证据状态的断言」而非「对事实的断言」，都该反查；但分级相反，见 `recheck`。
RECHECKED_LABELS: tuple[str, ...] = (UNCERTAIN_LABEL, SUSPICIOUS_LABEL)

# `存疑` 命中时随产物下发的口径提示。没有它，QC 会把建议级命中当阻断级处置，
# 把本来正确的 `存疑` 推成 `不符合` —— 那是错误排除，比漏判更贵。
UNCERTAIN_HITS_NOTE = (
    "以上为结论 `存疑` 且关键词在 OCR 命中的条目，**建议级**：命中只说明"
    "「该概念在病历里出现过」，回答的是 SKILL 原则十一 B 判据①与药物归类，"
    "**不回答判据②的针对性**（该治疗是否针对本条所述的病史/情形）。"
    "⛔ 不得据此把 `存疑` 直接推成 `不符合`；应据实改写 reason（承认该记录存在、"
    "说明它为何不满足本条），结论按三步判据重判——很可能仍是 `存疑`。"
)

# 从 `子条件` 派生关键词时要剔除的通用词。它们出现在几乎每条标准里，命中不构成
# "病历确实记录了这件事"的证据；不剔除会让每条研究者兜底条款都误报漏判。
# ⛔ 不要把「知情/同意/签署/生存/避孕/捐精」这类词放进来——它们正是这批条目的判据。
SUBCONDITION_STOPWORDS: frozenset[str] = frozenset(
    """
研究者 判断 患者 受试者 治疗 检查 记录 评估 方案 标准 以下 至少 包括 除外 必须 能够
进行 存在 相关 情况 其他 任何 以及 或者 并且 可能 需要 使用 发生 出现 已知 临床 试验
药物 期间 结束 要求 目的 充分 良好 愿意 具有 数值 阈值 正常 上限 下限 不限 具体 明确
未见 已查 缺失 补充 资料 病历 病史 首次 给药 本条 该条 条件 项目 时间 日期 之前 之后
以内 以上 之一 任一 严重 中度 轻度 活动 显著 参加 认为 适合
""".split()
)

# 动宾短语的谓语与连接词。标准写"签署知情同意书"，病历常写"知情同意书签署=…"——
# 词序一反，子串 grep 就跨不过去。因此再切一刀，把宾语名词单独取出来做关键词
# （"并签署知情同意书" → "知情同意书"），才能命中真实病历的表述。
# ⛔ 不要把「同意」放进来：它是"知情同意书"的组成部分，切掉就没有判据了。
_PHRASE_SPLITTERS: tuple[str, ...] = (
    "签署", "接受", "采取", "患有", "完成", "提供", "表示", "服用", "输注", "接种",
    "并", "且", "或", "及", "与", "的", "等", "和",
)


def subcondition_keywords(item: dict) -> list[str]:
    """从 `子条件`/`原文` 派生 grep 关键词。

    「不可从病例获取」条目的 `转化条件` 按 criteria-parser 契约恒为 `null`，没有
    `同义词`/`匹配字段` 可用。但"病历里没有该事实的客观记录"是需要**核查**才能得出的结论，
    不能因为分类是"不可获取"就跳过 —— 真实故障（S042002 IN-1 知情同意）正是这样漏掉的：
    病历里明写"知情同意书签署=2026-04-15 16:21"，却被判无法判断。

    两遍切分：
      1. 按标点与通用词切开，保留剩余的领域实词片段；
      2. 再按谓语/连接词切一刀，取出宾语名词（应对标准与病历的词序差异）。
    两遍结果都保留（并集）：多几个关键词只影响召回，精度由通用词表兜住。
    ⛔ 不用滑动 n-gram —— 中文 2 字窗会切出跨词垃圾，把通用词也变成"可命中"。
    """
    text = " ".join(_as_list(item.get("子条件")) + _as_list(item.get("原文")))
    out: set[str] = set()
    for m in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text):
        if len(m) >= 2:
            out.add(m)
    stop_pattern = "|".join(sorted((re.escape(w) for w in SUBCONDITION_STOPWORDS), key=len, reverse=True))
    phrase_pattern = "|".join(sorted((re.escape(w) for w in _PHRASE_SPLITTERS), key=len, reverse=True))
    for seg in re.split(r"[^\u4e00-\u9fff]+", text):
        for piece in re.split(stop_pattern, seg):
            piece = piece.strip()
            if len(piece) >= 2:
                out.add(piece)
                for sub in re.split(phrase_pattern, piece):
                    sub = sub.strip()
                    if len(sub) >= 2 and sub not in SUBCONDITION_STOPWORDS:
                        out.add(sub)
    return sorted(out)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _iter_criteria(criteria: dict):
    """遍历四分类，yield (条件ID, 条目dict)。

    类目形态：dict（key=条件ID，当前形态）或 list（旧 workspace，只读兼容）。
    形态合法性由 check_track_structure.py 闸13 上游阻断，这里只归一。
    """
    four = criteria.get("四分类", {})
    for _category, items in four.items():
        if isinstance(items, dict):
            entries: list = list(items.values())
        elif isinstance(items, list):
            entries = items
        else:
            continue
        for item in entries:
            if isinstance(item, dict) and item.get("条件ID"):
                yield str(item["条件ID"]), item


def build_keywords(item: dict) -> list[str]:
    """为一条可从病例获取条件构建 grep 关键词集合。

    来源：转化条件.同义词 + 匹配字段 + 内置量表同义词。去重、保序。
    """
    keywords: list[str] = []
    transform = item.get("转化条件") or {}
    if isinstance(transform, dict):
        keywords.extend(_as_list(transform.get("同义词")))
        keywords.extend(_as_list(transform.get("匹配字段")))
    # 内置量表同义词：任一匹配字段/子条件命中内置键 → 展开
    haystack = " ".join(_as_list(item.get("匹配字段")) + _as_list((item.get("转化条件") or {}).get("匹配字段")) + _as_list(item.get("子条件")) + _as_list(item.get("原文"))).lower()
    for key, syns in BUILTIN_SCALE_SYNONYMS.items():
        if key in haystack:
            keywords.extend(syns)
    # 去重保序（大小写不敏感去重）
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        norm = kw.strip().lower()
        if norm and norm not in seen:
            seen.add(norm)
            result.append(kw.strip())
    return result


def _iter_document_judgments(judgments: dict):
    """yield (document_key, 判定条目表)。统一判定产物（顶层 judgments，键 ""）优先；
    历史多 documents 产物兼容。doc_key 为空字符串表示统一证据源（无物料限定）。"""
    top = judgments.get("judgments")
    if isinstance(top, dict) and top:
        yield "", top
        return
    documents = judgments.get("documents") or {}
    if not isinstance(documents, dict):
        return
    for doc_key, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        entries = doc.get("judgments")
        if isinstance(entries, dict):
            yield doc_key, entries


def _collect_uncertain_judgments(judgments: dict):
    """yield (条件ID, document_key, judgment_dict) for 结论 ∈ `RECHECKED_LABELS` 的条目。

    `无法判断` 与 `存疑` 都是「对证据状态的断言」，都可能被 OCR 里实际存在的记录证伪，
    所以都进反查；但分级相反（见 `recheck`）。`符合`/`不符合` 是「对事实的断言」，不在范围内。
    """
    for doc_key, entries in _iter_document_judgments(judgments):
        for cid, jdg in entries.items():
            if isinstance(jdg, dict) and jdg.get("conclusion") in RECHECKED_LABELS:
                yield str(cid), doc_key, jdg


def _load_ocr_lines(ocr_paths: list[Path]) -> list[tuple[str, int, str]]:
    """返回 [(source_label, line_no(1-based), line_text), ...]。"""
    lines: list[tuple[str, int, str]] = []
    for p in ocr_paths:
        if not p.exists():
            continue
        label = p.parent.name or p.name
        for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            lines.append((label, i, raw))
    return lines


def grep_keywords(ocr_lines, keywords: list[str], max_hits: int = 10):
    """大小写不敏感子串检索，返回命中行列表。

    两类命中被排除（Task 8 误报收紧）：
    - 化验单**参考范围**行（`男≤26` / `0-7`）：那是量表的列，不是该患者的记录；
    - 仅由**宽泛类别短语**命中的行（`新型内分泌治疗`）：命中往往是标准原文的复述。
      同一行若还命中了具体药名（`阿比特龙`），仍算命中。
    """
    if not keywords:
        return []
    patterns = [(kw, re.compile(re.escape(kw), re.IGNORECASE)) for kw in keywords]
    specific = [(kw, pat) for kw, pat in patterns if not is_broad_class_phrase(kw)]
    hits = []
    for source, line_no, text in ocr_lines:
        matched = [(kw, pat) for kw, pat in specific if pat.search(text)]
        if not matched:
            continue
        if _looks_like_reference_range(text):
            continue
        snippet = text.strip()
        if len(snippet) > 200:
            # 命中长/密集段落：截取命中词周边，避免噪音淹没证据
            for _kw, pat in matched:
                m = pat.search(text)
                if m:
                    start = max(0, m.start() - 40)
                    end = min(len(text), m.end() + 40)
                    snippet = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
                    break
        hits.append({"source": source, "line_no": line_no, "text": snippet})
        if len(hits) >= max_hits:
            break
    return hits


def _count_judgments(judgments: dict) -> int:
    """判定文件里**能读到**的判定条目总数（不论结论）。

    与 `checked`（只数「无法判断」条目）区分开：`checked == 0` 既可能是本轨确实没有无法判断
    条目（正常），也可能是判定文件结构读不出来（严重）。会话 9a83ccc9 里判定子代理把顶层
    `judgments` 写成列表，本闸读到 0 条却报「反查通过」，子代理据此回报 "gates pass"。
    """
    return sum(1 for _doc_key, entries in _iter_document_judgments(judgments) for v in entries.values() if isinstance(v, dict))


def _document_matches(doc_key: str, source: str) -> bool:
    """命中来源是否就是该判定条目所属的文档。

    会话 `d393714d` step 148-152：关键词命中的是「筛选期病历」，却被标到「筛选期检查」条目的
    `hit=True` 上，agent 花大量步数证明这是误报。OCR 汇总文件的 `source` 取自其父目录名，
    与 `documents` 的键同源（`phase2_summary.json.ocr_results[].source`），因此可直接比对；
    双向 `in` 兜住「筛选期病历」vs「筛选期病历p1-8」这类带页码后缀的差异。
    """
    a, b = (doc_key or "").strip(), (source or "").strip()
    if not a or not b:
        return True  # 信息不足时不做跨文档判定，交由原有逻辑处理
    return a == b or a in b or b in a


def _split_hits_by_document(doc_key: str, hits: list[dict], ocr_sources: set[str]) -> tuple[list[dict], list[dict]]:
    """把命中拆成 (本文档, 其它文档)。

    ⚠️ **只有在 `doc_key` 确实对应某个 OCR 来源标签时才做这个区分。** OCR 汇总文件的 source 取自
    父目录名，理论上与 `documents` 的键同源（都来自 `phase2_summary.json.ocr_results[].source`），
    但一旦命名体系不对应（例如目录名根本不是文档名），"哪个都不匹配"就必须退化为**不过滤**，
    而不是把所有命中都丢掉——本闸静默失效比误报严重得多。
    """
    if not any(_document_matches(doc_key, source) for source in ocr_sources):
        return list(hits), []
    same: list[dict] = []
    other: list[dict] = []
    for hit in hits:
        if _document_matches(doc_key, hit.get("source", "")):
            same.append(hit)
        else:
            other.append(hit)
    return same, other


def _load_history(path: Path | None) -> tuple[list[dict], list[str]]:
    """读取轮次账本，返回 (rounds, notes)。账本坏掉时安全重置——它是加固项，不是主闸。"""
    if path is None or not path.exists():
        return [], []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], [f"{path.name} 不合法，轮次账本已重置并重新计数"]
    if not isinstance(loaded, list):
        return [], [f"{path.name} 不是轮次列表，轮次账本已重置并重新计数"]
    return [r for r in loaded if isinstance(r, dict)], []


def _consecutive_repeats(rounds: list[dict], key: str, current: list[str], current_hash: str) -> int:
    """当前集合在账本尾部连续出现的次数（含本轮）。

    集合变化即归零。若判定文件哈希未变（说明子代理重新运行了门禁但未修改产物），
    则该轮按 2 次计数以加速熔断 —— 防止子代理在脚本调试上浪费 token。
    """
    if not current:
        return 0
    count = 1
    for previous in reversed(rounds):
        if previous.get(key) == current:
            # 哈希未变 = 子代理重跑脚本但没改产物 → 加速计数
            if previous.get("judgments_input_hash") == current_hash:
                count += 2
            else:
                count += 1
        else:
            break
    return count


def _print_about() -> None:
    """输出脚本核心逻辑摘要，供 AI 代理理解门禁行为而不需读源码。"""
    print(
        """\
## uncertain_recheck.py — 确定性漏判反查门禁

**这是什么**：机械关键词检索脚本。对每条判定为「无法判断」且 `可从病例获取=true` 的条件，
用该条件的同义词/匹配字段在 OCR 文本中做大小写不敏感 grep。命中 → 证据其实存在 → 漏判。

**不是做什么的**：不修改判定文件、不写判定结论、不自动改判。它是只读的诊断工具。

### 输入/输出
- 输入：criteria_parsed.json（标准包，取条件同义词和匹配字段）+ judgments_draft.json（判定结论）+ ocr_records.md（患者 OCR 文本）
- 输出：`suspected_missed`（漏判条目列表，必须清空）+ `uncertain_hits`（建议级，存疑条目命中）+ `judgments_input_hash`（判定文件 SHA256 前 12 位）

### 核心行为
- `main()` 从磁盘 json.load() 读取判定文件 → 传给 `recheck()`
- `recheck()` 接受内存 dict，每次调用在 history 文件中记录本轮 `suspected_missed` 集合
- 同一批 `suspected_missed` 连续 3 轮未清 → 熔断（exit 3，`stuck_items` 非空）
- `judgments_input_hash` 相同 → 判定文件未被修改，重新运行毫无意义（加速计数 ×2）

### 当坏循环发生时
如果 `suspected_missed` 反复出现且 `judgments_input_hash` 不变：
→ **判定文件没被改过**。需要修改 prod 的 `conclusion`/`reason`/`evidence` 字段，
  而不是反复运行本脚本。本脚本只是读取并报告，不会改变它自己的输出。

### 禁止操作
- 不要删除 `.uncertain_recheck_history.json`（会重置熔断计数器）
- 不要删除 `__pycache__/`（Python 自动生成，删除无效）
- 不要调试 `main()` vs `recheck()` 的行为差异（差异来自输入来源不同：磁盘 vs 内存）
- 熔断时（exit 3 / stuck_items）上报主代理，不要继续修改"""
    )


def recheck(criteria: dict, judgments: dict, ocr_paths: list[Path], *, history_path: Path | None = None) -> dict:
    criteria_map = dict(_iter_criteria(criteria))
    ocr_lines = _load_ocr_lines(ocr_paths)
    entries = []
    suspected = []
    uncertain_hits = []
    notes: list[str] = []
    ocr_sources = {source for source, _lineno, _text in ocr_lines}
    for cid, doc_key, jdg in _collect_uncertain_judgments(judgments):
        item = criteria_map.get(cid)
        if not item:
            continue
        # 覆盖**全部**「无法判断」条目，包括 `可从病例获取=false` 的。
        # 曾经在此按可获取性豁免（注释写着"不可获取（如知情同意）跳过"），
        # 直接导致真实漏判：S042002 的 IN-1「自愿参加临床试验并签署知情同意书」被判无法判断，
        # 而筛选期病历里明写"知情同意书签署=2026-04-15 16:21…自愿参加本研究"。
        # "病历里没有该事实的客观记录"必须**核查后**才能成立，不能由前置分类替代核查。
        obtainable = bool(item.get("可从病例获取"))
        conclusion = str(jdg.get("conclusion") or "")
        keywords = build_keywords(item)
        anchor_source = "转化条件" if keywords else "子条件"
        if not keywords:
            keywords = subcondition_keywords(item)
        all_hits = grep_keywords(ocr_lines, keywords)
        # 只有**本文档**的命中才构成本条目的漏判；其它文档的命中留痕供 QC 参考，
        # 但不进 suspected_missed（见 `_document_matches`）。
        hits, cross_document_hits = _split_hits_by_document(doc_key, all_hits, ocr_sources)
        hit = bool(hits)
        entry = {
            "条件ID": cid,
            "document": doc_key,
            "conclusion": conclusion,
            "可从病例获取": obtainable,
            "anchor_source": anchor_source,
            "keywords": keywords,
            "hit": hit,
            "grep_hits": hits,
        }
        if cross_document_hits:
            entry["cross_document_hits"] = cross_document_hits
        if not keywords:
            # 派生不出可用关键词（子条件全是通用词，如纯"研究者判断"）。显式标记，
            # 不让"查不了"伪装成"查过且没有"。
            entry["no_keywords"] = True
            entry["anchor_source"] = "无"
        entries.append(entry)
        if not hit:
            continue
        # 分级：`无法判断` 命中 = 该判「符合/不符合/存疑」却判了「没内容」→ 阻断级漏判；
        # `存疑` 命中 = 判定方已承认「有相关内容但不足定论」，命中不与该结论矛盾 → 建议级，
        # 只要求据实改写 reason（会话 `1fee1395` EX-1-3：结论对、reason 的缺失断言假）。
        if conclusion == UNCERTAIN_LABEL:
            if cid not in suspected:
                suspected.append(cid)
        elif cid not in uncertain_hits:
            uncertain_hits.append(cid)

    rounds, history_notes = _load_history(history_path)
    notes.extend(history_notes)
    judgments_hash = hashlib.sha256(
        json.dumps(judgments, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]
    blocking_repeats = _consecutive_repeats(rounds, "suspected_missed", suspected, judgments_hash)
    advisory_repeats = _consecutive_repeats(rounds, "uncertain_hits", uncertain_hits, judgments_hash)

    result = {
        "patient_id": judgments.get("patient_id"),
        "checked": len(entries),
        "judgments_seen": _count_judgments(judgments),
        "unreadable_judgments": _count_judgments(judgments) == 0,
        "suspected_missed": suspected,
        "uncertain_hits": uncertain_hits,
        "entries": entries,
        "judgments_input_hash": judgments_hash,
    }
    if uncertain_hits:
        result["uncertain_hits_note"] = UNCERTAIN_HITS_NOTE

    # ⛔ 阻断级只允许「失败上报」，不允许静默降级——否则熔断就成了漏判的合法出口。
    stuck: list[str] = []
    if blocking_repeats >= ESCALATION_ROUNDS:
        stuck = list(suspected)
        result["gate_escalated"] = True
        result["escalation_level"] = "blocking"
        result["escalation_note"] = BLOCKING_ESCALATION_NOTE.format(rounds=blocking_repeats, items=", ".join(stuck))
    elif advisory_repeats >= ESCALATION_ROUNDS:
        result["gate_escalated"] = True
        result["escalation_level"] = "advisory"
        result["escalation_note"] = ADVISORY_ESCALATION_NOTE.format(rounds=advisory_repeats, items=", ".join(uncertain_hits))
    result["stuck_items"] = stuck
    result["rounds_unchanged"] = {"suspected_missed": blocking_repeats, "uncertain_hits": advisory_repeats}
    if notes:
        result["notes"] = notes

    if history_path is not None:
        rounds.append({
            "suspected_missed": suspected,
            "uncertain_hits": uncertain_hits,
            "judgments_input_hash": result["judgments_input_hash"],
        })
        # 有界：账本只用来判断"最近连续几轮相同"，留最近 N 轮足够。
        rounds = rounds[-MAX_HISTORY_ROUNDS:]
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(json.dumps(rounds, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            result.setdefault("notes", []).append(f"轮次账本写入失败：{history_path}")
    return result


def main(argv=None) -> int:
    # --about 在 argparse 之前处理，避免 required 参数检查
    if argv is None:
        argv = sys.argv[1:]
    if "--about" in argv:
        _print_about()
        return 0

    ap = argparse.ArgumentParser(description="确定性无法判断反查兜底闸（O1）")
    ap.add_argument("--criteria", required=True, help="criteria_parsed.json 路径")
    ap.add_argument("--judgments", required=True, help="judgments_draft.json 路径")
    ap.add_argument("--ocr", required=True, nargs="+", help="该患者各来源 ocr_records.md 路径（可多个）")
    ap.add_argument("--out", required=True, help="输出 uncertain_recheck.json 路径")
    ap.add_argument(
        "--history",
        help="轮次账本路径（默认与 --out 同目录的 uncertain_recheck_history.json）。同一批 suspected_missed 连续 3 轮未清即熔断。",
    )
    ap.add_argument(
        "--about",
        action="store_true",
        help="输出脚本核心逻辑摘要（供 AI 代理理解，不需读源码）并退出",
    )
    args = ap.parse_args(argv)

    criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
    judgments = json.loads(Path(args.judgments).read_text(encoding="utf-8"))
    ocr_paths = [Path(p) for p in args.ocr]
    out_path = Path(args.out)
    history_path = Path(args.history) if args.history else out_path.with_name(f"{out_path.stem}_history.json")

    result = recheck(criteria, judgments, ocr_paths, history_path=history_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for note in result.get("notes", []):
        print(f"ℹ️ {note}")

    if result["unreadable_judgments"]:
        print(
            "⛔ 判定文件里读不到任何判定条目（`documents.{source}.judgments` 不是「条件ID → 条目」的嵌套 dict）——"
            "本闸无法反查，「反查通过」在此情形下不成立。\n"
            "  先跑 check_judgment_structure.py 定位结构问题（形态见 references/judgment-schema.md）；"
            "结构不对时回派判定重出产物，⛔ 不要在畸形产物上转码修复。",
            file=sys.stderr,
        )
        print(f"产物已写入：{out_path}")
        return 2

    if result.get("escalation_level") == "blocking":
        # exit 3：与 exit 2（结构不可读）区分开。这里结构是好的，问题是「改了 3 轮还是同一批」，
        # 唯一正确的动作是上报失败，而不是让 agent 继续改产物。
        print(result["escalation_note"])
        print(f"stuck_items = {result['stuck_items']}")
        print(f"产物已写入：{out_path}")
        return 3

    if result["suspected_missed"]:
        rounds_unchanged = result["rounds_unchanged"]["suspected_missed"]
        # 从 history 文件读取上一轮的 hash，检测判定文件是否未变化
        current_hash = result["judgments_input_hash"]
        previous_hash = None
        if history_path.exists():
            try:
                history_data = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(history_data, list) and len(history_data) >= 2:
                    previous_hash = history_data[-2].get("judgments_input_hash")
            except (json.JSONDecodeError, OSError):
                pass
        if previous_hash and previous_hash == current_hash and rounds_unchanged >= 2:
            print(
                f"⚠️ 疑似漏判（证据在 OCR 却判无法判断）：{result['suspected_missed']} —— "
                "必须 read_file 命中行上下文后据实改判，禁止保留「无法判断」。"
                f"（同一批已连续 {rounds_unchanged} 轮未清；"
                f"连续 {ESCALATION_ROUNDS} 轮相同将熔断并要求上报失败）"
            )
            print(
                f"⚠ 判定文件自上次检查以来未发生变化（hash={current_hash}）。"
                f"suspected_missed 条目需要修改判定文件的 conclusion/reason 字段，"
                f"而非重新运行本脚本。请使用 apply_json_patches 或 write_file 修改判定文件后重新运行。"
            )
        else:
            print(
                f"⚠️ 疑似漏判（证据在 OCR 却判无法判断）：{result['suspected_missed']} —— "
                "必须 read_file 命中行上下文后据实改判，禁止保留「无法判断」。"
                f"（同一批已连续 {rounds_unchanged} 轮未清；"
                f"连续 {ESCALATION_ROUNDS} 轮相同将熔断并要求上报失败）"
            )
    else:
        print(
            f"✅ 反查通过：结论「无法判断」的条目均无关键词命中，可保留"
            f"（本次反查 {result['checked']} 条「无法判断」+「存疑」，判定条目共 {result['judgments_seen']} 条）。"
        )
    if result["uncertain_hits"]:
        print(
            f"ℹ️ 建议级 —— 结论「存疑」但关键词在 OCR 命中：{result['uncertain_hits']}。"
            "命中只回答 SKILL 原则十一 B 的判据①与药物归类，**不回答判据②的针对性**；"
            "⛔ 不必也不得据此直接改判为「不符合」，但 reason 必须据实改写"
            "（承认该记录存在、说明它为何不满足本条）。"
        )
    if result.get("escalation_level") == "advisory":
        print(result["escalation_note"])
    print(f"产物已写入：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
