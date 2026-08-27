#!/usr/bin/env python3
"""reason ↔ 条件ID 对齐机械闸（按患者 × 轨）。

判定条目数守恒、结论方向、漏判反查都已有闸，但**没有任何闸校验"这条 reason 是不是在讲
这个条件ID 对应的条件"**。这是 thread `81562273`（患者 M018）整批错位能一路走到交付报告
的原因：结构闸只看条目数与 JSON 结构，方向闸只看排除项措辞，漏判闸只看「无法判断」，
`merge-reasons` 只做机械回填 —— 四道闸对"张冠李戴"全盲。

实测故障（M018）：
- IN 轨 24 条中 16 条错位：IN-6（PSMA 阳性）配"ECOG 1 分"、IN-7（RECIST 可测量病灶）配
  "预计生存期>6 个月"、IN-11（白蛋白≥30 g/L）配"病毒/梅毒筛查"；IN-10-8 配了标准里
  根本不存在的"凝血 INR/APTT"子条件。
- reason 引用该患者 OCR 命中 0 次的化验值：ANC 3.55 / PLT 206 / HGB 133 / 肌酐 80.1
  （真实 PLT 136 / HGB 121 / 肌酐 64 / TBIL 7.5），其中 133、80.1 只见于**其他患者**。
- EX 轨 draft 亦有 7 条错位，且 EX-13-1 与 EX-13-2 的 reason 逐字相同（复制粘贴）。

## 判据来源：标准包自带锚点，不引入新的语义判断

每个条件在 `criteria_judge_{TRACK}.json` 里都带 `转化条件.同义词 / 匹配字段 / 或条件`，
它们就是"这条条件在病历里长什么样"的权威词表。本闸只做**确定性子串匹配**，不做 LLM 推理：

1. **闸A 串轨（`cross_condition_reason`）** —— reason 未命中本条任何锚点，却命中了**其他条件
   独有**的锚点 → 张冠李戴的确证（M018 的 EX-11 配 EX-15 理由即此类）。
2. **闸B 锚点零命中（`no_anchor_hit`）** —— 有可用锚点但 reason 一个都没命中 → 该 reason
   没在讲这个条件。锚点取自标准包时为**阻断级**；取自 `子条件` 回退时为**建议级**
   （catch-all 兜底条款的条件本身是开放式的，词法上永远对不齐，硬阻断只会逼出绕闸行为）。
3. **闸C 数值无据（`unsourced_number`）** —— reason 里的数值既不在本条 `evidence`、也不在
   该患者 OCR、也不是标准自带数字（核素名/阈值）、也不是紧跟 ≥≤<> 的阈值复述、也不是
   `judgments.judgment_date`（判定当天）→ 编造或跨患者污染。
   > `judgment_date` 白名单：时间窗条件（「签署知情同意书前 6 个月内…」）的参考日期缺失时按
   > **判定当天**兜底，而该日期来自系统时钟、天然不在病历里。不放过它，每条走兜底的时间窗
   > 条件都会被本闸误判成编造数值并阻断合并。只放过这一个值本身，换别的日期照旧抓。
4. **闸D reason 逐字重复（`duplicate_reason`）** —— 同一轨内多个条件ID 的 reason 完全相同：
   不同条件不可能有逐字相同的理由，这是复制粘贴/模板填充的确证，必有条目未被真正判定。
5. **`empty_reason`** —— reason 空白。
6. **闸E 读不到条目（`unreadable_judgments`）** —— 标准包非空但判定文件里一条条目都读不出来
   （`documents.{source}.judgments` 不是「条件ID → 条目」的嵌套 dict）→ 本闸无法核验，
   **不得报「全过」**。会话 `9a83ccc9`：判定子代理把顶层 `judgments` 写成列表（自创 schema），
   `_flatten` 返回 `{}`，本闸 `checked=0 / conflicts=[]` 报全过，子代理据此回报 "gates pass"，
   而唯一能识破该文件的结构闸恰好被委派 prompt 漏掉了。产物另附 `coverage`（核验数/标准包条数），
   覆盖不全时出**建议级** `partial_coverage`（定性仍归结构闸闸2）。
7. **闸G 事件零命中却因缺日期悬置（`window_moot_absence`）** —— 时间窗条件的事件在 OCR 里
   零命中、reason 也断言它不存在，结论却因「缺参考日期」停在无法判断/存疑：**事件不存在时
   任何参考日期都不能让它落进窗口**。**建议级**（锚点零命中 ≠ 事件不存在，见函数 docstring）。

无可用锚点的条目（如"研究者判断"这类纯通用词条件）记入 `skipped`，**不阻断**——宁可漏报
也不误伤，误报会让主代理学会绕闸。

M018 实测（对照人工标注）：IN 轨 16 条真错位命中 15（漏 IN-5：其理由提到「软组织/骨」，
与条件词面重叠但未引用「进展」证据，属语义层缺陷，超出词法闸能力）；EX 轨 7 条真错位
全部命中；另抓出 7 条编造数值、8 条复制粘贴理由；**对齐条目零误报**。

用法：
    python3 check_reason_alignment.py --criteria .../criteria_judge_IN.json \
        --judgments .../judgments_draft_{id}_IN.json \
        --ocr .../ocr_records.md [--ocr 第二份...] \
        --out .../reason_alignment_{id}_IN.json --patient {id} --track IN

exit 0 = 全过；exit 2 = 有阻断项（禁止派 QC、禁止进入合并）。
⛔ 本脚本只产出诊断，**不自动改判**；改判方式见 `references/judgment-repair.md`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 通用词：出现在几乎所有条件里，命中它们不构成"讲了这个条件"的证据。
# ⛔ 不要把「措施/水平/能力/计划/结果/范围」放进来——它们是真实术语的构词成分
#（避孕措施、去势水平、生育能力、捐精计划），列为停用词会把复合术语切碎、削弱锚点。
STOPWORDS = frozenset(
    """
研究者 判断 患者 受试者 治疗 检查 记录 评估 方案 标准 以下 至少 包括 除外 必须 能够
进行 存在 相关 情况 其他 任何 以及 或者 并且 可能 需要 使用 发生 出现 已知 临床 试验
药物 签署 开始 期间 结束 要求 目的 充分 良好 愿意 具有 数值 阈值
正常 上限 下限 不限 具体 明确 未见 已查 缺失 补充 资料 病历 病史 首次 给药
本条 该条 条件 项目 时间 日期 之前 之后 以内 以上 之一 任一 严重 中度 轻度 活动 显著
""".split()
)

# 数值前若紧跟比较符，视为标准阈值复述（含单位换算），不要求在病历中命中
_CMP_CHARS = "≥≤<>=～~＞＜"


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def transform_anchors(item: dict) -> list[str]:
    """标准包自带锚点：同义词 + 或条件 + 匹配字段。"""
    t = item.get("转化条件") or {}
    raw = _as_list(t.get("同义词")) + _as_list(t.get("或条件")) + _as_list(t.get("匹配字段"))
    return sorted({a.strip() for a in raw if a and len(a.strip()) >= 2 and a.strip() not in STOPWORDS})


def subcondition_anchors(item: dict) -> list[str]:
    """回退锚点：从 `子条件` 抽候选关键词。

    「不可从病例获取」类条目通常没有 `转化条件`（无匹配字段/同义词），此时用子条件文本兜底。
    做法：先按标点与**通用词**切开，剩下的连续片段就是领域实词（长度 ≥2）。
    ⛔ 不用滑动 n-gram —— 中文 2 字窗会切出「究者」「者判」这类跨词垃圾，反而造成偶然命中
    （实测会让"研究者判断"类纯通用词条件产出可匹配锚点，闸失效）。
    """
    text = str(item.get("子条件") or "")
    out: set[str] = set()
    for m in re.findall(r"[A-Za-z][A-Za-z0-9\-]+|\d+[A-Za-z%]+", text):
        if len(m) >= 2:
            out.add(m)
    # 通用词与非中文字符一律作为分隔符
    pattern = "|".join(sorted((re.escape(w) for w in STOPWORDS), key=len, reverse=True))
    for seg in re.split(r"[^\u4e00-\u9fff]+", text):
        for piece in re.split(pattern, seg):
            piece = piece.strip()
            if len(piece) >= 2:
                out.add(piece)
    return sorted(out)


def _anchor_hit(anchor: str, reason: str) -> bool:
    """锚点命中：整词命中，或（≥4 字锚点）其 3 字窗命中。

    3 字窗用于容忍构词差异（子条件「高效避孕措施」vs 理由「避孕措施」）。
    2-3 字锚点要求整词命中，避免短片段偶然撞上。
    """
    if anchor in reason:
        return True
    if len(anchor) >= 4:
        return any(anchor[i : i + 3] in reason for i in range(len(anchor) - 2))
    return False


def anchors_for(item: dict) -> tuple[list[str], str]:
    """返回 (锚点列表, 来源)；优先用标准包锚点，缺失才回退子条件。"""
    anc = transform_anchors(item)
    if anc:
        return anc, "transform"
    return subcondition_anchors(item), "subcondition"


def condition_text(item: dict) -> str:
    """条件自带文本（用于识别"数值来自标准而非病历"）。"""
    t = item.get("转化条件") or {}
    parts = [str(item.get("子条件") or ""), str(item.get("原文") or ""), str(t.get("阈值") or "")]
    parts += _as_list(t.get("同义词")) + _as_list(t.get("或条件")) + _as_list(t.get("匹配字段"))
    return " ".join(parts)


# 需要外部知识才能确定的药物/治疗归类术语。这类归属读不出病历，得查证；
# 而归类一错就直接翻转患者的入排结果，所以 reason 声称了归类就必须留下依据。
_DRUG_CLASS_TERMS: tuple[str, ...] = (
    "全身性药物治疗",
    "全身性治疗",
    "全身治疗",
    "系统性治疗",
    "新型内分泌治疗",
    "紫杉类",
    "紫衫类",
    "小分子靶向",
    "免疫抑制剂",
    "免疫检查点抑制剂",
    "强效CYP3A4",
    "CYP3A4抑制剂",
    "CYP3A4诱导剂",
    "抗肿瘤中药",
    "抗肿瘤中成药",
    "活疫苗",
    "减毒活疫苗",
)

# evidence 里出现任一标记即视为"归类有据可查"（外部来源或病历自述皆可）。
_SOURCE_MARKERS: tuple[str, ...] = ("http://", "https://", "归类依据", "来源", "依据：", "参考", "说明书", "指南")

# ── 概念级类别→具体药名映射（`false_absence_claim` 用）────────────────────
# reason 写的是**类别**（"无全身性糖皮质激素处方"），OCR 写的是**药名**（"地塞米松"）。
# 不桥接就永远对不上，缺失断言的真伪也就无法机械证伪。
# 三处必须一致：本表、`criteria-parser/SKILL.md` 的内置同义词对照、`uncertain_recheck.py` 的兜底表。
_CLASS_TO_DRUGS: dict[str, tuple[str, ...]] = {
    "全身性糖皮质激素": ("地塞米松", "泼尼松", "泼尼松龙", "甲泼尼龙", "氢化可的松", "倍他米松", "曲安西龙", "可的松"),
    "糖皮质激素": ("地塞米松", "泼尼松", "泼尼松龙", "甲泼尼龙", "氢化可的松", "倍他米松", "曲安西龙", "可的松"),
    "皮质激素": ("地塞米松", "泼尼松", "泼尼松龙", "甲泼尼龙", "氢化可的松", "倍他米松"),
    "皮质类固醇": ("地塞米松", "泼尼松", "泼尼松龙", "甲泼尼龙", "氢化可的松", "倍他米松"),
    "生物制剂": ("奥马珠单抗", "度普利尤单抗", "美泊利珠单抗", "单抗"),
    "紫杉类": ("多西他赛", "紫杉醇", "白蛋白紫杉醇", "卡巴他赛", "多帕菲"),
    "紫衫类": ("多西他赛", "紫杉醇", "白蛋白紫杉醇", "卡巴他赛"),
    "新型内分泌治疗": ("阿比特龙", "恩扎卢胺", "氘恩扎如胺", "阿帕他胺", "达罗他胺", "瑞维鲁胺"),
    "免疫抑制剂": ("环孢素", "他克莫司", "硫唑嘌呤", "甲氨蝶呤", "霉酚酸酯", "环磷酰胺"),
    "免疫检查点抑制剂": ("帕博利珠单抗", "纳武利尤单抗", "信迪利单抗", "替瑞利尤单抗", "阿替利珠单抗"),
    "减毒活疫苗": ("麻疹疫苗", "水痘疫苗", "口服轮状", "黄热", "卡介苗", "脊灰减毒"),
    "活疫苗": ("麻疹疫苗", "水痘疫苗", "黄热", "卡介苗"),
    "G-CSF": ("非格司亭", "培非格司亭", "瑞白", "粒细胞集落刺激因子"),
}

# reason 断言"这类东西不存在"的句式。只认这些，避免把正面陈述（"长期口服全身性糖皮质激素"）
# 当成缺失断言。
_ABSENCE_PATTERNS: tuple[str, ...] = ("未见", "未找到", "未出现", "未提及", "未记录", "无", "没有", "缺")


def false_absence_claims(reason: str, ocr_text: str) -> list[tuple[str, list[str]]]:
    """reason 断言某类药物/治疗不存在，而该类别的具体药名在 OCR 里命中。

    动机（会话 `1fee1395` EX-1-3）：结论 `存疑` 正确，但 reason 写「无哮喘用药记录、
    **无全身性糖皮质激素或生物制剂处方**」，而 OCR 明写 `2025.04.09起地塞米松及青霉素治疗`。
    结论对、理由假，此前没有任何闸校验缺失断言的真伪。

    **建议级**：按 SKILL 原则十一 B 的三步判据，地塞米松治的是尿频尿急、落不到判据②，
    所以 `存疑` 仍然正确 —— 要改的是 reason 的措辞（承认药在、说明为何不算），不是结论。
    硬阻断会把判定方逼向改判，方向就反了。

    返回 `[(类别, [OCR 里命中的药名...]), ...]`。
    """
    reason = reason or ""
    ocr_text = ocr_text or ""
    if not reason or not ocr_text:
        return []
    out: list[tuple[str, list[str]]] = []
    for klass, drugs in _CLASS_TO_DRUGS.items():
        idx = reason.find(klass)
        if idx < 0:
            continue
        # 只看该类别词**之前**的一小段：缺失词必须紧挨着它，"长期口服全身性糖皮质激素"
        # 这种正面陈述前面没有缺失词。
        window = reason[max(0, idx - 12) : idx]
        if not any(pat in window for pat in _ABSENCE_PATTERNS):
            continue
        hits = [d for d in drugs if d in ocr_text]
        if not hits:
            continue  # 缺失断言为真
        # reason 已经承认该药存在（改写后的形态）→ 不再报
        if any(d in reason for d in hits):
            continue
        out.append((klass, hits))
    # 类别词互相包含时只留最长的那个（"全身性糖皮质激素" ⊃ "糖皮质激素" ⊃ "皮质激素"），
    # 否则同一句缺失断言会被报三遍，读者以为有三个问题。
    return [
        (k, h)
        for k, h in out
        if not any(other != k and k in other and set(h) == set(oh) for other, oh in out)
    ]


# 「缺参考日期」作为悬置理由的措辞。命中其一才认为该判定是因缺日期而未定论。
_MISSING_DATE_PATTERNS = (
    "未找到知情同意",
    "缺少知情同意",
    "缺失知情同意",
    "未记载",
    "未明确记载",
    "无法确定时间窗",
    "无法精确界定",
    "无法计算",
    "无法判定",
    "参考日期缺失",
    "缺少参考日期",
    "缺失的参考日期",
    "日期未在",
    "缺少筛选日期",
    "需补充筛选日期",
    "缺少首次给药日期",
)

# reason 断言「事件不存在」的措辞。⚠️ 必须搭配「时间窗」类关键词一起判，
# 否则会把 EX-6「手术有记载、只是不知在不在窗内」误当成事件不存在。
_EVENT_ABSENT_PATTERNS = (
    "未找到任何",
    "未见任何",
    "无任何",
    "未找到",
    "未见",
    "未记录",
    "未发现",
    "全文检索未见",
    "均未见",
    "无相关记录",
)

# IN 轨的**负向**子条件标志（「未接受输血」这类）。正向要求（「判断为 PSMA 阳性」）
# 缺了检查就是真的无法判断，不在本闸范围。
_NEGATIVE_REQUIREMENT_MARKERS = ("未接受", "未使用", "未进行", "未曾", "不可接受", "未接种", "无需", "未服用", "未曾使用")

# 事件锚点不取 `匹配字段` —— 那是字段名（如「抗肿瘤治疗史」），拿它去 OCR 里撞
# 「治疗」二字会几乎必然命中，把本闸变成永不触发。
_MIN_ANCHOR_LEN = 2


def event_anchors(item: dict) -> list[str]:
    """时间窗条件里「被查找的事件」的锚点：`转化条件.阈值`（离散取值）+ `同义词`。"""
    t = item.get("转化条件") or {}
    raw: list[str] = []
    threshold = t.get("阈值")
    if isinstance(threshold, list):
        raw += [str(v) for v in threshold]
    raw += _as_list(t.get("同义词"))
    seen: dict[str, None] = {}
    for a in raw:
        a = a.strip()
        if len(a) >= _MIN_ANCHOR_LEN:
            seen.setdefault(a, None)
    return list(seen)


def window_moot_absence(item: dict, entry: dict, ocr_text: str, track: str) -> dict | None:
    """事件零命中 → 时间窗不适用 → 缺参考日期不构成悬置理由。

    真实故障 `d1883294` 的 EX-2-2：核素治疗/半身放疗在病历里一个都没有，却因为
    「找不到知情同意书签署日期」判了 `无法判断`。**事件不存在时，任何参考日期都不能
    让它落进窗口** —— 缺的那个日期对结论没有任何影响。

    三条判据必须同时成立（每一条都是真实数据上的假阳边界，缺一条就误报）：

    1. 该条有 `日期维度.时间窗`，且 `conclusion ∈ {无法判断, 存疑}`；
    2. 事件锚点在 OCR 里**零命中**，且 reason 自己也断言事件不存在 ——
       只看锚点不够：`EX-6` 的「重大外科治疗」措辞与病历「冷冻切除术」对不上，
       锚点零命中而事件其实存在，reason 也写着有记载；
    3. EX 轨，或 IN 轨的**负向**子条件。IN 轨正向要求（`IN-6`「判断为 PSMA 阳性」）
       缺了检查就是真的无法判断。

    返回 advisory 载荷或 `None`。**建议级**：锚点词法判断不足以自动改判，
    但足以逼判定方回答「既然事件不存在，为什么还悬着」。
    """
    window = str(((item.get("日期维度") or {}).get("时间窗")) or "").strip()
    if not window:
        return None
    if entry.get("conclusion") not in ("无法判断", "存疑"):
        return None

    subcondition = str(item.get("子条件") or "")
    if track.upper() == "IN" and not any(m in subcondition for m in _NEGATIVE_REQUIREMENT_MARKERS):
        return None

    reason = str(entry.get("reason") or "")
    if not any(p in reason for p in _MISSING_DATE_PATTERNS):
        return None
    if not any(p in reason for p in _EVENT_ABSENT_PATTERNS):
        return None

    anchors = event_anchors(item)
    if not anchors:
        return None
    hits = [a for a in anchors if a and a in ocr_text]
    if hits:
        return None

    verdict = "符合（未触发）" if track.upper() == "EX" else "符合"
    return {
        "type": "window_moot_absence",
        "finding": (
            f"reason 断言该事件不存在，OCR 对 {len(anchors)} 个事件锚点也零命中，"
            f"却因缺参考日期把结论悬置在「{entry.get('conclusion')}」"
        ),
        "detail": (
            f"时间窗「{window}」在事件不存在时无从适用 —— 任何参考日期都不能让一个"
            f"不存在的事件落进窗口，缺的那个日期对结论没有影响"
        ),
        "action": (
            f"复核是否应改判「{verdict}」：若穷尽检索后该事件确实零命中，"
            f"按此定论并在 reason 写明「事件本身零命中，故时间窗不适用」，"
            f"⛔ 不得再以「缺参考日期」为悬置理由（SKILL「日期/时间窗判定」C 条只管"
            f"「事件发生了但日期查不到」）。若事件其实存在、只是措辞与锚点不同，"
            f"则改写 reason 引用那段原文，本项自然消失。"
        ),
        "anchors_checked": anchors,
        "time_window": window,
    }


def unsourced_drug_class_claims(reason: str, evidence_text: str) -> list[str]:
    """reason 声称了药物/治疗归类，但 evidence 里找不到任何来源标记。

    建议级而非阻断级：归类也可能直接来自病历自述（如病历写明"全身性激素治疗"），
    机械上难以与外部查证区分，硬阻断会误伤。交 QC 核实归类依据。

    ⚠️ 归类结论只回答"这药属于哪一类"，**不回答"本条是否被触发"**。
    真实案例（M018）：病历"处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺"——氘恩扎如胺是
    恩扎卢胺类雄激素受体抑制剂（治去势抵抗性前列腺癌），是研究者因筛选失败而改用的替代**抗肿瘤**
    方案，并非哮喘的治疗药。把它当作"仍需全身性药物治疗"的依据去触发"有变态反应病史且仍需全身性
    药物治疗"，逻辑不成立：肿瘤试验患者几乎都在接受全身抗肿瘤治疗，按那种读法该排除标准会排掉
    100% 的候选者。"该治疗是否针对本条所述疾病"必须由语义判断，本闸只管"归类有没有依据"。
    """
    claims = [term for term in _DRUG_CLASS_TERMS if term in (reason or "")]
    if not claims:
        return []
    if any(marker in (evidence_text or "") for marker in _SOURCE_MARKERS):
        return []
    return claims


_HEDGE_MARKERS = ("约", "大约", "近", "左右", "上下", "上限", "下限", "估计", "推算", "换算", "折算", "以上", "以下", "不足", "超过")

# ⚠️ 这里**故意不做** OCR 乱码的自动识别。试过两种模式都不可用：`\d[|]\d` 会命中表格分隔符
# （`81.0|40-75%`），`\d[A-Za-z]` 会命中所有带单位的正常数值（`1000mg`、`26U/L`）——两者都会把
# 真正的编造数值静默降级，那比误报贵得多。乱码豁免只走**显式标注** `ocr_corrupted=true`：
# 它由判定方声明、QC 可复核，责任明确。


def _is_hedged_number(reason: str, start: int, end: int) -> bool:
    """该数值是否处在**解释性表述**里（"上限约 111"），而非判定依据。

    判据取数值前后各 6 字的窗口：模糊词只可能紧贴它出现。范围放宽会把"ALT 18 U/L（参考≤26）"
    这种真依据也算成解释性。
    """
    window = reason[max(0, start - 6) : min(len(reason), end + 6)]
    return any(marker in window for marker in _HEDGE_MARKERS)


def unsourced_numbers(
    reason: str,
    cond_text: str,
    evidence_text: str,
    ocr_text: str,
    judgment_date: str = "",
    ocr_corrupted: bool = False,
) -> tuple[list[str], list[str]]:
    """reason 中既无实据、也非标准自带的数值，按严重程度分两档返回 `(blocking, advisory)`。

    `judgment_date`（判定当天）白名单化：时间窗条件的参考日期缺失时按判定当天兜底，
    而这个日期来自系统时钟、**天然不在病历里**。不放过它，每条走兜底的时间窗条件都会被本闸
    误判为编造数值 → conflicts 非空 → 禁止派 QC / 禁止合并。白名单只放过 `judgment_date`
    这一个值本身，换任何其他未见于病历的日期照旧抓。

    **分档（Task 9）**：
    - **阻断级**：判定依据数值。改错结论就靠它，必须逐字溯源。
    - **建议级**：① 解释性表述里的数值（"上限约 111"、"约 3 个月"）——它不承载判定，
      只是行文；② 该条目**显式标注** `ocr_corrupted=true`（原文乱码如 `57-11um01/1`，
      "字面出现"这个要求本身不成立）。建议级只要求据实改写措辞，**不阻断**流程。
    """
    blocking: set[str] = set()
    advisory: set[str] = set()
    garbled_ocr = bool(ocr_corrupted)
    allowed_date_nums = {m.group(0) for m in re.finditer(r"\d+", str(judgment_date or ""))}
    for m in re.finditer(r"\d+(?:\.\d+)?", reason or ""):
        num = m.group(0)
        if len(num) < 2 and "." not in num:
            continue  # 单字符数字噪声（第1条、0~1级）
        prefix = reason[max(0, m.start() - 2) : m.start()]
        if any(c in prefix for c in _CMP_CHARS):
            continue  # 阈值复述
        if num in cond_text or num in evidence_text or (ocr_text and num in ocr_text):
            continue
        if num in allowed_date_nums:
            continue  # 判定当天（兜底参考日期）
        if _is_hedged_number(reason, m.start(), m.end()) or garbled_ocr:
            advisory.add(num)
            continue
        blocking.add(num)
    return sorted(blocking), sorted(advisory)


def _flatten(judgments: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    top = judgments.get("judgments")
    if isinstance(top, dict):  # 统一判定产物：顶层 judgments
        for cid, e in top.items():
            if isinstance(e, dict):
                out[str(cid)] = e
        return out
    docs = judgments.get("documents")
    if isinstance(docs, dict):  # 历史多 documents 产物兼容
        for doc in docs.values():
            if isinstance(doc, dict):
                for cid, e in (doc.get("judgments") or {}).items():
                    if isinstance(e, dict):
                        out[str(cid)] = e
    return out


def _items_of(criteria: dict) -> dict[str, dict]:
    """条件ID → 条目。类目形态 dict（当前）或 list（旧 workspace，只读兼容）。"""
    out: dict[str, dict] = {}
    for arr in (criteria.get("四分类") or {}).values():
        entries = list(arr.values()) if isinstance(arr, dict) else arr
        if not isinstance(entries, list):
            continue
        for o in entries:
            if isinstance(o, dict) and o.get("条件ID"):
                out[str(o["条件ID"])] = o
    return out


def check(
    criteria: dict,
    judgments: dict,
    *,
    ocr_text: str = "",
    patient: str = "",
    track: str = "",
    scope_ids: set[str] | None = None,
) -> dict:
    """`scope_ids`：本次判定应覆盖的条件ID 集合（分批判定时是本批清单），None = 整轨。

    ⚠️ 只收窄**覆盖率口径**（`coverage` 与 `partial_coverage`），**不收窄锚点表**：
    `unique_anchors` 必须按**整轨**建，否则检不出 `cross_condition_reason` —— 理由讲的是
    别的条件时，那个条件很可能不在本批里，把锚点表也切到本批等于让串轨归因失灵，
    而这正是本闸最主要的捕获目标（thread `81562273`：24 条里 16 条错位）。
    """
    items = _items_of(criteria)
    entries = _flatten(judgments)
    # 覆盖率的分母。分批时只问「本批该判的判齐了没」；整轨时问「整轨判齐了没」。
    scope = {cid for cid in (scope_ids or items) if cid in items} if scope_ids else set(items)
    # 判定当天：时间窗条件的兜底参考日期。由主代理在委派模板里给定并落盘于此，
    # 供闸C 白名单使用（见 unsourced_numbers 的 docstring）。
    judgment_date = str(judgments.get("judgment_date") or judgments.get("判定日期") or "")

    anchor_map: dict[str, list[str]] = {}
    source_map: dict[str, str] = {}
    for cid, item in items.items():
        anchor_map[cid], source_map[cid] = anchors_for(item)

    # 独有锚点：只属于单一条件的锚点词，用于串轨归因
    freq = Counter(a for anc in anchor_map.values() for a in anc)
    unique_anchors = {cid: [a for a in anc if freq[a] == 1] for cid, anc in anchor_map.items()}

    # reason 逐字重复（同一轨内）
    norm = {cid: re.sub(r"\s+", "", str(e.get("reason") or "")) for cid, e in entries.items()}
    dup_counts = Counter(v for v in norm.values() if v)
    dup_texts = {v for v, n in dup_counts.items() if n > 1}

    report: dict = {
        "patient_id": patient,
        "track": track,
        "checked": 0,
        "entries_seen": len(entries),
        "coverage": f"0/{len(scope)}",
        "coverage_scope": ("批次清单" if scope_ids else "整轨标准包") + f"（{len(scope)} 条）",
        "conflicts": [],
        "advisories": [],
        "skipped": [],
        "entries": [],
        "exit_code": 0,
    }

    # 读不到任何判定条目 → 自己报阻断，不假定结构闸会兜。
    # 会话 9a83ccc9：判定子代理把顶层 `judgments` 写成列表（自创 schema），`_flatten` 返回 {}，
    # 于是 checked=0 / conflicts=[] → 本闸报「全过」，子代理据此回报 "gates pass"；
    # 而唯一能识破该文件的结构闸恰好被委派 prompt 漏掉，错误一路带到合并阶段。
    if scope and not entries:
        report["conflicts"].append(
            {
                "condition_id": "*",
                "type": "unreadable_judgments",
                "finding": (
                    f"判定文件里读不到任何判定条目（本次应覆盖 {len(scope)} 条），"
                    "本闸无法核验；conclusion=「全过」在此情形下不成立"
                ),
                "action": (
                    "先跑 check_judgment_structure.py 确认 `documents.{source}.judgments` "
                    "为「条件ID → 条目」的嵌套 dict（形态见 references/judgment-schema.md）；"
                    "结构不对时回派判定重出产物，⛔ 不要在畸形产物上转码修复"
                ),
            }
        )

    for cid in sorted(items, key=lambda c: (len(c), c)):
        entry = entries.get(cid)
        if entry is None:
            continue  # 条目缺失由结构闸（闸1/闸2）负责，不在本闸重复报
        report["checked"] += 1
        reason = str(entry.get("reason") or "")
        anc = anchor_map[cid]
        evidence_text = json.dumps(entry.get("evidence") or [], ensure_ascii=False)
        rec: dict = {"条件ID": cid, "anchor_source": source_map[cid], "anchor_count": len(anc)}

        if not reason.strip():
            report["conflicts"].append(
                {
                    "condition_id": cid,
                    "type": "empty_reason",
                    "finding": "reason 为空",
                    "action": "补写该条 reason（「无法判断」须含三要素：已查范围 + 缺失的具体信息 + 可解除条件）",
                }
            )
            rec["status"] = "empty_reason"
            report["entries"].append(rec)
            continue

        if norm[cid] in dup_texts:
            others = sorted(c for c, v in norm.items() if v == norm[cid] and c != cid)
            report["conflicts"].append(
                {
                    "condition_id": cid,
                    "type": "duplicate_reason",
                    "finding": f"reason 与 {others} 逐字相同（复制粘贴/模板填充）",
                    "action": "逐条重写：说明本条各自查了哪些字段、缺的是哪个具体指标",
                    "same_as": others,
                }
            )
            rec["status"] = "duplicate_reason"

        hits = [a for a in anc if _anchor_hit(a, reason)]
        if not anc:
            report["skipped"].append(cid)
            rec["status"] = rec.get("status") or "skipped_no_anchor"
        elif hits:
            rec["status"] = rec.get("status") or "aligned"
            rec["anchor_hits"] = hits[:5]
        else:
            cross = sorted({o for o, uniq in unique_anchors.items() if o != cid and any(_anchor_hit(a, reason) for a in uniq)})
            if cross:
                report["conflicts"].append(
                    {
                        "condition_id": cid,
                        "type": "cross_condition_reason",
                        "finding": f"reason 未命中本条锚点，却命中 {cross} 的独有锚点 → 条件ID 与理由错位",
                        "action": f"改写为针对本条（{items[cid].get('子条件') or ''}）的判定理由；若结论也是照另一条写的，须一并改判 conclusion",
                        "matched_other": cross,
                        "expected_anchors": anc[:8],
                    }
                )
                rec["status"] = "cross_condition_reason"
                rec["matched_other"] = cross
            else:
                finding = {
                    "condition_id": cid,
                    "type": "no_anchor_hit",
                    "finding": f"reason 未命中本条任何锚点 {anc[:8]} → 该理由可能没在讲这个条件",
                    "action": f"改写为针对本条（{items[cid].get('子条件') or ''}）的判定理由，并引用对应字段/指标名",
                    "expected_anchors": anc[:8],
                    "anchor_source": source_map[cid],
                }
                # 锚点来自 `子条件` 回退时证据较弱 → 建议级。catch-all 兜底条款（如「研究者认为
                # 受试者存在其他可能影响依从性或不适合参加本试验的情况」）的条件本身是开放式的，
                # 天生无法通过词法对齐；判成阻断会每轮必阻、逼着主代理学会绕闸，反而毁掉闸的可信度。
                # 标准包自带锚点（transform）是 criteria-parser 策展过的权威词表，零命中即阻断。
                if source_map[cid] == "transform":
                    report["conflicts"].append(finding)
                else:
                    report["advisories"].append(finding)
                rec["status"] = "no_anchor_hit"

        bad_nums, hedged_nums = unsourced_numbers(
            reason,
            condition_text(items[cid]),
            evidence_text,
            ocr_text,
            judgment_date,
            ocr_corrupted=bool(entry.get("ocr_corrupted")),
        )
        if bad_nums:
            report["conflicts"].append(
                {
                    "condition_id": cid,
                    "type": "unsourced_number",
                    "finding": f"reason 引用的数值 {bad_nums} 既不在本条 evidence、也不在该患者 OCR",
                    "action": "按 OCR 原文改为真实数值并补 evidence 引用；若本就无该项检查，改判为「无法判断」并写明缺失项",
                    "numbers": bad_nums,
                }
            )
            rec["unsourced_numbers"] = bad_nums
            if rec.get("status") in (None, "aligned", "skipped_no_anchor"):
                rec["status"] = "unsourced_number"
        if hedged_nums:
            # 建议级：解释性表述的数值，或 OCR 本身乱码导致「字面出现」这个要求不成立。
            # ⛔ 不进 conflicts —— 会话 `2d628340` 的 IN-10-8「111」就是被当阻断级，
            # 逼出连续 10 轮「改数字措辞绕闸」，对判定质量毫无增益。
            report["advisories"].append(
                {
                    "condition_id": cid,
                    "type": "unsourced_number_hedged",
                    "finding": f"reason 里的数值 {hedged_nums} 未逐字出现在 evidence/OCR，但处于解释性表述中或该条 OCR 存在乱码",
                    "action": "据实改写措辞（说明该数值是换算/估计值及其来源），或标 ocr_corrupted 说明原文乱码；⛔ 不要为了通过本项而删掉数值",
                    "numbers": hedged_nums,
                }
            )
            rec["unsourced_numbers_hedged"] = hedged_nums

        class_claims = unsourced_drug_class_claims(reason, evidence_text)
        if class_claims:
            report["advisories"].append(
                {
                    "condition_id": cid,
                    "type": "unsourced_drug_class",
                    "finding": f"reason 声称了药物/治疗归类 {class_claims}，但 evidence 里没有任何来源标记",
                    "action": (
                        "用 web_search 查证该药物/治疗的归类（查询串只放药物名与类别术语，⛔ 不得含任何患者信息），"
                        "把结论与来源写进 evidence；若归类来自病历自述，则引用该病历原文。"
                        "⚠️ 另需确认该治疗是**针对本条所述疾病**的——患者恰好在用的全身抗肿瘤药不能用来满足"
                        "「仍需全身性药物治疗」（否则该排除标准会排掉全部肿瘤试验候选者）"
                    ),
                    "claims": class_claims,
                }
            )
            rec["unsourced_drug_class"] = class_claims

        absence = false_absence_claims(reason, ocr_text)
        if absence:
            detail = "；".join(f"称无「{klass}」，但 OCR 有 {hits}" for klass, hits in absence)
            report["advisories"].append(
                {
                    "condition_id": cid,
                    "type": "false_absence_claim",
                    "finding": f"reason 的缺失断言与 OCR 矛盾：{detail}",
                    "action": (
                        "改写 reason：先承认该药/治疗在病历里，再说明它为什么不满足本条 —— "
                        "按 SKILL 原则十一 B 的三步判据，②要求该治疗**针对本条所述的病史/情形**，"
                        "治别的病的全身性药物落不到②。⚠️ 本项**不要求改判 conclusion**："
                        "结论很可能仍然正确，错的只是「未见…」这句事实陈述。"
                        "⛔ 不得靠删掉这句话过闸——缺口本身要写清（缺的是「针对该病史的治疗方案记录」）。"
                    ),
                    "claims": [{"类别": klass, "OCR命中": hits} for klass, hits in absence],
                }
            )
            rec["false_absence_claim"] = [{"类别": k, "OCR命中": h} for k, h in absence]

        # 事件零命中 → 时间窗不适用。与上面的 `false_absence_claim` **互斥**：
        # 前者说「你称无、OCR 却有」，本项说「你称无、OCR 也确实无 —— 那就别悬着」。
        moot = None if absence else window_moot_absence(items[cid], entry, ocr_text, track)
        if moot:
            report["advisories"].append({"condition_id": cid, **moot})
            rec["window_moot_absence"] = moot["time_window"]

        report["entries"].append(rec)

    report["coverage"] = f"{report['checked']}/{len(scope)}"
    # 覆盖率只按 `scope` 算。分批判定时整轨包里必然有一堆不属于本批的条件，按整轨算会让
    # **每一批**都稳定报一次 partial_coverage —— 一个恒真的告警等于没有告警，
    # 真正的「本批漏判了一条」会淹没在噪声里。
    missing = sorted(cid for cid in scope if cid not in entries)
    if missing:
        # 定性归结构闸闸2（键集合恒等于期望集合），但覆盖率必须暴露在本闸产物里：
        # 否则「只核了 1/64 条」与「64 条全过」在报告上长得一模一样。
        scope_label = report["coverage_scope"]
        report["advisories"].append(
            {
                "condition_id": "*",
                "type": "partial_coverage",
                "finding": f"应覆盖 {len(scope)} 条（口径={scope_label}），其中 {len(missing)} 条在判定文件中缺失：{missing[:10]}",
                "action": "跑 check_judgment_structure.py（闸2 判定条目集合须恒等于期望集合；分批时加 --batch N）",
            }
        )
    report["conflicts"].sort(key=lambda c: (str(c["condition_id"]), c["type"]))
    report["exit_code"] = 2 if report["conflicts"] else 0
    return report


def _summary(report: dict) -> str:
    by_type = Counter(c["type"] for c in report["conflicts"])
    head = (
        f"reason 对齐闸 {report['patient_id']} {report['track']}：核验 {report['checked']} 条"
        f"（覆盖 {report.get('coverage', '?')}"
        + (f"，口径={report['coverage_scope']}" if report.get("coverage_scope") else "")
        + f"），阻断 {len(report['conflicts'])} 项，跳过（无可用锚点）{len(report['skipped'])} 条"
    )
    if not by_type:
        return head + " → 全过"
    detail = "；".join(f"{t}={n}" for t, n in sorted(by_type.items()))
    ids = sorted({str(c["condition_id"]) for c in report["conflicts"]})
    return head + f"\n  分类：{detail}\n  条件ID：{ids}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验 reason 是否真的在讲该条件ID 对应的条件")
    ap.add_argument("--criteria", required=True, help="本轨标准包 criteria_judge_{TRACK}.json")
    ap.add_argument("--judgments", required=True, help="本轨判定 judgments_draft_{id}_{TRACK}.json")
    # action="extend" + nargs="+": 兼容 `--ocr A B`（空格分隔，与 uncertain_recheck.py
    # 一致）与 `--ocr A --ocr B`（重复旗标，旧形态）两种写法。历史事故 f9231297：
    # 子代理按空格分隔形态调用本脚本 → unrecognized arguments → 误读为「只接受单个
    # --ocr」→ 单文件重跑 → 对齐闸只核一半 OCR 的「半失明」状态下报 conflicts=[]。
    ap.add_argument("--ocr", action="extend", nargs="+", default=[], help="该患者 ocr_records.md（双文档传两个：--ocr A B，或 --ocr A --ocr B）")
    ap.add_argument("--out", required=True, help="输出 reason_alignment_{id}_{TRACK}.json")
    ap.add_argument("--patient", default="", help="患者ID（写入报告）")
    ap.add_argument("--track", default="", choices=["", "IN", "EX"], help="轨（写入报告）")
    ap.add_argument(
        "--condition-ids",
        nargs="+",
        default=None,
        help="分批判定：本批应覆盖的条件ID（取自 judge_pack.py plan-batches 的 condition_ids）。"
        "只收窄覆盖率口径，锚点表仍按整轨建（串轨归因需要它）。不给 = 整轨口径。",
    )
    args = ap.parse_args(argv)

    criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
    judgments = json.loads(Path(args.judgments).read_text(encoding="utf-8"))
    ocr_text = ""
    for p in args.ocr:
        path = Path(p)
        if path.exists():
            ocr_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")

    report = check(
        criteria,
        judgments,
        ocr_text=ocr_text,
        patient=args.patient,
        track=args.track,
        scope_ids=set(args.condition_ids) if args.condition_ids else None,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_summary(report))
    print(f"→ {out}")
    if report["conflicts"]:
        print("⛔ 阻断项非空：禁止派 QC、禁止进入合并；改判后重跑本闸至清空。", file=sys.stderr)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
