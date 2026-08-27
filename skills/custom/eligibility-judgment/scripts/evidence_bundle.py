#!/usr/bin/env python3
"""QC 取证素材**预装配**：把逐条 grep + read 的取证过程压成一次 read（按患者 × 轨）。

## 为什么需要它：贵的不是"读了多少"，是"读完还要被重传多少次"

会话 `93d8a2c6` 实测（26.6M token）：

- input 占 **98.9%**（26,259,864 / 300,020），而所有 subagent step 的**独立内容**合计只有
  ~956k token —— 同一份内容平均被重发了 **18 遍**。最重的判定 task 更极端：唯一内容 121k、
  计费 input 3.65M，**30×**。
- 成本模型（与实测吻合）：**计费 input ≈ (AI 步数 / 2) × 该 task 累积内容量**。
  一次 read 发生在第 5 步，它的正文会被后面每一步各重传一次。
- 所以「把 grep 窗口开大」是**反向**优化：payload 变大后要乘以剩余步数。
  实测也印证读取路径不是瓶颈 —— 266 次读里范围重叠浪费只有 6%（1,341 / 22,233 行）。

真正的乘数是**步数**。失败的那个 QC task（`IN judgment QC round 2`，1.97M token）是标本：
106 步 / 50 个 AI 步，做的事是 **31 次 read_file + 15 次 grep + 1 次 ls，零个闸脚本** ——
逐条核验 30 多个条目，每条都要 grep 找证据、再 read 上下文。取证内容本身很小，
贵在它分散于 60 多步、每步都背着此前所有步的历史，最后耗尽 `max_turns=150` 的额度而失败。

本脚本把这 60 多步的取证**一次装配好**：QC 读一次产物就拿到全部素材，后续核验几乎不再
需要工具调用。按成本模型，(60/2)×payload → (5/2)×payload。

## 它装配什么（每个条目一块）

1. 条件原文 + 标准包自带锚点（`同义词` / `匹配字段` / `或条件`）——判断"该查什么"的依据；
2. 当前判定：`conclusion` / `exclusion_triggered` / `或组` + `或组语义`；
3. `reason` 原文；
4. **已引用 evidence 的逐字核验结果**：每条 `quote` 能否在该患者 OCR 中找到，找到则给行号。
   ⛔ 这是 QC 最常自己 grep 去做的事，也最该机械化 —— 引文真实性是确定性判断，不是语义判断。
5. **OCR 命中窗口**：按锚点检索该患者 OCR，给出命中行 ± 上下文（重叠窗口自动合并）。

## 它**不**做什么

⛔ 不下任何判定结论、不改任何产物、不替代语义 QC。它只把"素材"摆到桌上；
`qc-delegation.md` 里那些语义核验项（方向语义复核、从严判断、归类对象正确）仍由 QC 自己做。
⛔ 也不替代 `check_reason_alignment.py` / `check_judgment_structure.py`：那些是**判据闸**，
输出的是"哪条不合格"；本脚本输出的是"核这条需要的原文"。两者互补，前者结论 + 后者素材。

## 用法

    python3 /mnt/skills/custom/eligibility-judgment/scripts/evidence_bundle.py \
        --criteria  /mnt/user-data/workspace/criteria_judge_IN.json \
        --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_IN.json \
        --ocr       /mnt/user-data/workspace/patients/{id}/ocr/筛选期检查/ocr_records.md \
        --ocr       /mnt/user-data/workspace/patients/{id}/ocr/筛选期病历/ocr_records.md \
        --out       /mnt/user-data/workspace/patients/{id}/evidence_bundle_{id}_IN.md \
        --patient {id} --track IN

exit 0 = 已装配（即便存在未核验通过的引文，那是 QC 的判断对象，不是本脚本的失败）；
exit 2 = 输入不可读/结构不可用（判定文件或标准包缺失、JSON 坏掉）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# 每个条目最多给多少条命中行。上限存在的理由和脚本本身一样：产物自己不能变成新的上下文炸弹。
MAX_HITS_PER_ENTRY = 8
# 命中行上下文窗口（前后各 N 行）。⚠️ 不要为了"看得更全"调大：装配产物会被后续每一步重传。
CONTEXT_LINES = 3
# 单个条目块的字符上限，超出则截断并标注。
MAX_BLOCK_CHARS = 2600
# 整份产物的字符上限。超限时保留全部条目的"判定 + 引文核验"，只截命中窗口。
MAX_BUNDLE_CHARS = 90000

# 宽泛类别短语：拿它们检索会命中标准原文的复述而非患者记录。与 uncertain_recheck.py 同源意图。
_BROAD_ANCHORS = frozenset(
    {
        "治疗",
        "用药",
        "药物",
        "检查",
        "病史",
        "既往",
        "手术",
        "感染",
        "疾病",
        "异常",
        "功能",
        "指标",
        "内分泌治疗",
        "新型内分泌治疗",
        "化疗",
        "放疗",
        "抗肿瘤治疗",
        "系统性治疗",
    }
)


def _norm(s: str) -> str:
    """空白全删 + NFKC，用于引文的逐字包含性比对。

    OCR 与 `quote` 之间的空格/换行/全半角差异是提取工艺噪声，不是引文造假。抹掉它们再比，
    否则本脚本会把大量真实引文报成"找不到"，QC 反而要去人工复核每一条 —— 步数又回来了。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_ocr_lines(ocr_paths: list[Path]) -> list[tuple[str, int, str]]:
    """返回 [(source_label, line_no(1-based), line_text), ...]。

    与 `uncertain_recheck.py` 的加载方式保持一致（source 取父目录名），这样两个脚本给出的
    行号与来源标签可以互相对照。
    """
    lines: list[tuple[str, int, str]] = []
    for p in ocr_paths:
        if not p.exists():
            continue
        label = p.parent.name or p.name
        for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            lines.append((label, i, raw))
    return lines


def flatten_criteria(criteria: dict) -> dict[str, dict]:
    """条件ID → 条件条目。兼容 `四分类` 与扁平 `条件` 两种形态。

    `四分类` 的类目本身也有两种形态：dict（key=条件ID，当前形态）与 list（旧 workspace，
    只读兼容）。形态合法性由 check_track_structure.py 闸13 上游阻断。
    """
    items: dict[str, dict] = {}
    buckets = criteria.get("四分类")
    if isinstance(buckets, dict):
        for group in buckets.values():
            entries = list(group.values()) if isinstance(group, dict) else group
            if isinstance(entries, list):
                for it in entries:
                    if isinstance(it, dict) and it.get("条件ID"):
                        items[str(it["条件ID"])] = it
    for key in ("条件", "items"):
        group = criteria.get(key)
        if isinstance(group, list):
            for it in group:
                if isinstance(it, dict) and it.get("条件ID"):
                    items[str(it["条件ID"])] = it
    return items


def flatten_judgments(judgments: dict) -> list[tuple[str, str, dict]]:
    """返回 [(document_key, 条件ID, entry), ...]，保持文件内出现顺序。

    统一判定产物（顶层 judgments）的 doc_key 为 ""（无物料限定）；历史多 documents 兼容。
    """
    out: list[tuple[str, str, dict]] = []
    top = judgments.get("judgments")
    if isinstance(top, dict):
        for cid, entry in top.items():
            if isinstance(entry, dict):
                out.append(("", str(cid), entry))
        return out
    docs = judgments.get("documents")
    if isinstance(docs, dict):
        for doc_key, doc in docs.items():
            entries = (doc or {}).get("judgments")
            if isinstance(entries, dict):
                for cid, entry in entries.items():
                    if isinstance(entry, dict):
                        out.append((str(doc_key), str(cid), entry))
    return out


def anchors_for(item: dict) -> list[str]:
    """该条件的检索锚点，去掉宽泛类别短语与过短词。

    锚点来自标准包自带的 `转化条件`，不是本脚本猜的 —— 与 `check_reason_alignment.py` 闸A/闸B
    同一套词表，因此两个脚本对"该查什么"的口径一致。
    """
    t = item.get("转化条件") or {}
    raw: list[str] = []
    for key in ("同义词", "匹配字段", "或条件"):
        value = t.get(key)
        if isinstance(value, list):
            raw += [str(v) for v in value if v]
        elif isinstance(value, str) and value:
            raw.append(value)
    seen: dict[str, None] = {}
    for a in raw:
        a = a.strip()
        if len(a) < 2 or a in _BROAD_ANCHORS:
            continue
        seen.setdefault(a, None)
    return list(seen)


def find_quote(quote: str, ocr_lines: list[tuple[str, int, str]]) -> tuple[str, int] | None:
    """引文能否在 OCR 中逐字找到（空白/全半角归一后）。返回 (source, line_no)。

    先逐行匹配；命中不了再在**整份文本**上匹配 —— 表格类引文常跨行，逐行必然找不到。
    跨行命中时返回起始行的坐标。
    """
    needle = _norm(quote)
    if not needle:
        return None
    for source, lineno, text in ocr_lines:
        if needle in _norm(text):
            return (source, lineno)
    by_source: dict[str, list[tuple[int, str]]] = {}
    for source, lineno, text in ocr_lines:
        by_source.setdefault(source, []).append((lineno, text))
    for source, rows in by_source.items():
        joined = _norm("".join(t for _, t in rows))
        if needle not in joined:
            continue
        offset = joined.index(needle)
        seen = 0
        for lineno, text in rows:
            seen += len(_norm(text))
            if seen > offset:
                return (source, lineno)
    return None


def hit_lines(anchors: list[str], ocr_lines: list[tuple[str, int, str]]) -> list[tuple[str, int]]:
    """锚点命中的 (source, line_no)，每条目上限 MAX_HITS_PER_ENTRY。"""
    if not anchors:
        return []
    patterns = [re.compile(re.escape(a), re.IGNORECASE) for a in anchors]
    hits: list[tuple[str, int]] = []
    for source, lineno, text in ocr_lines:
        if any(p.search(text) for p in patterns):
            hits.append((source, lineno))
            if len(hits) >= MAX_HITS_PER_ENTRY:
                break
    return hits


def merge_windows(all_hits: set[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """把**全部条目**的命中行合并成互不重叠的窗口，返回 [(source, start, end), ...]。

    ⚠️ 合并必须是**跨条目**的。按条目各自出窗口时，相邻条目的窗口会大面积重叠（实测
    IN-10-2 给 L1-6、IN-10-3 给 L2-8、IN-7 给 L4-10，第 4/5/6 行被贴了三遍），30 个条目
    就是三倍 payload —— 而这份产物会被后续每一步重传。所以窗口全局去重，条目只引用编号。
    """
    windows: list[tuple[str, int, int]] = []
    for source, lineno in sorted(all_hits):
        start, end = max(1, lineno - CONTEXT_LINES), lineno + CONTEXT_LINES
        if windows and windows[-1][0] == source and start <= windows[-1][2] + 1:
            windows[-1] = (source, windows[-1][1], max(windows[-1][2], end))
        else:
            windows.append((source, start, end))
    return windows


def windows_for(hits: list[tuple[str, int]], windows: list[tuple[str, int, int]]) -> list[int]:
    """该条目的命中落在哪些窗口里（1-based 编号，去重保序）。"""
    ids: list[int] = []
    for source, lineno in hits:
        for n, (wsource, start, end) in enumerate(windows, start=1):
            if wsource == source and start <= lineno <= end and n not in ids:
                ids.append(n)
                break
    return ids


def render_window(source: str, start: int, end: int, ocr_lines: list[tuple[str, int, str]]) -> list[str]:
    out = []
    for s, lineno, text in ocr_lines:
        if s == source and start <= lineno <= end:
            out.append(f"{lineno}: {text.rstrip()}")
    return out


def _truncate(text: str, limit: int) -> str:
    """截断到 `limit` **字符以内，含截断标记本身**。

    ⚠️ 标记必须算进预算。第一版是先切到 limit 再拼标记，结果产物比上限多出标记那十几个字符 ——
    上限就成了摆设（单测 `test_bundle_total_is_capped…` 抓到 90,017 > 90,000）。
    """
    if len(text) <= limit:
        return text
    marker = f"…[截断，原 {len(text)} 字符]"
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def build_bundle(
    criteria: dict,
    judgments: dict,
    ocr_paths: list[Path],
    *,
    patient: str = "",
    track: str = "",
) -> tuple[str, dict]:
    """装配产物（Markdown）与统计信息。

    产物用 Markdown 而不是 JSON：消费者是 QC 子代理的**阅读**，不是程序解析。同样内容的
    JSON 要为中文与引号付转义开销，而这份产物会被后续每一步重传。
    """
    items = flatten_criteria(criteria)
    entries = flatten_judgments(judgments)
    ocr_lines = load_ocr_lines(ocr_paths)

    stats = {
        "条目数": len(entries),
        "引文总数": 0,
        "引文可溯源": 0,
        "引文未找到": 0,
        "无 evidence 的条目": [],
        "锚点缺失的条目": [],
        "OCR 行数": len(ocr_lines),
        "截断的条目": [],
    }

    head = [
        f"# QC 取证素材包 — 患者 {patient or judgments.get('patient_id') or '?'} / {track or '?'} 轨",
        "",
        "> 本文件由 `evidence_bundle.py` 机械装配，**一次读取即拿到全部取证素材**。",
        "> ⛔ 不要再逐条 `grep` + `read_file` 取证 —— 那会把核验拆成几十步，而每一步都要重传",
        "> 此前所有步的上下文（实测倍数 18×~30×）。需要更宽的窗口时，只对**个别**条目补读。",
        "> ⛔ 本文件不含任何判定结论；`引文核验` 一列是确定性的逐字比对结果，语义核验仍由你做。",
        "",
    ]

    blocks: list[str] = []
    # 两趟：先把所有条目的命中行收齐，全局合并成互不重叠的窗口，条目再引用编号。
    per_entry: list[tuple[str, str, dict, dict, list[str], list[tuple[str, int]]]] = []
    all_hits: set[tuple[str, int]] = set()
    for doc_key, cid, entry in entries:
        item = items.get(cid, {})
        anchors = anchors_for(item)
        if not anchors:
            stats["锚点缺失的条目"].append(cid)
        hits = hit_lines(anchors, ocr_lines)
        all_hits.update(hits)
        per_entry.append((doc_key, cid, entry, item, anchors, hits))
    windows = merge_windows(all_hits)

    for doc_key, cid, entry, item, anchors, hits in per_entry:
        lines = [f"## {cid}", ""]
        cond = str(item.get("子条件") or item.get("原文") or "").strip()
        if cond:
            lines.append(f"- **条件**：{_truncate(cond, 300)}")
        if anchors:
            lines.append(f"- **锚点**：{'、'.join(anchors[:12])}")
        lines.append(f"- **文档**：{doc_key}")
        verdict = f"- **当前判定**：`{entry.get('conclusion')}`"
        if "exclusion_triggered" in entry:
            verdict += f"　`exclusion_triggered={entry.get('exclusion_triggered')}`"
        if entry.get("或组"):
            verdict += f"　`或组={entry.get('或组')}`（{entry.get('或组语义') or '语义缺失'}）"
        lines.append(verdict)
        reason = str(entry.get("reason") or "").strip()
        lines.append(f"- **reason**：{_truncate(reason, 600) if reason else '（空）'}")

        evidence = entry.get("evidence")
        if isinstance(evidence, list) and evidence:
            lines += ["", "| # | source | page | quote | 引文核验 |", "|---|---|---|---|---|"]
            for n, ev in enumerate(evidence, start=1):
                if not isinstance(ev, dict):
                    lines.append(f"| {n} | — | — | ⛔ 非对象形态（闸12） | — |")
                    continue
                stats["引文总数"] += 1
                quote = str(ev.get("quote") or "")
                found = find_quote(quote, ocr_lines) if quote else None
                if found:
                    stats["引文可溯源"] += 1
                    check = f"✅ {found[0]}:{found[1]}"
                else:
                    stats["引文未找到"] += 1
                    check = "❌ OCR 中未找到"
                cell = _truncate(quote.replace("|", "\\|").replace("\n", " "), 120) or "（空）"
                lines.append(f"| {n} | {ev.get('source') or '—'} | {ev.get('page') or '—'} | {cell} | {check} |")
        else:
            stats["无 evidence 的条目"].append(cid)
            lines += ["", "- **evidence**：（空）"]

        ids = windows_for(hits, windows)
        if ids:
            refs = "、".join(f"[W{n}](#w{n})" for n in ids)
            hit_at = "、".join(f"{s}:{ln}" for s, ln in hits)
            lines += ["", f"- **OCR 命中**：{hit_at}　→ 窗口 {refs}"]
        elif anchors:
            lines += ["", "- **OCR 命中**：无（锚点在该患者 OCR 中零命中 —— 可能是真缺失，也可能是措辞不同）"]

        block = "\n".join(lines)
        if len(block) > MAX_BLOCK_CHARS:
            block = _truncate(block, MAX_BLOCK_CHARS)
            stats["截断的条目"].append(cid)
        blocks.append(block + "\n")

    appendix: list[str] = []
    if windows:
        appendix += [
            "## OCR 窗口",
            "",
            "> 全部条目的命中行**合并去重**后的原文窗口，每一行只出现一次。条目块里的 `W{n}` 指向这里。",
            "",
        ]
        for n, (source, start, end) in enumerate(windows, start=1):
            body = render_window(source, start, end, ocr_lines)
            if not body:
                continue
            appendix.append(f'<a id="w{n}"></a>**W{n}** `{source}` L{start}-{end}')
            appendix.append("```")
            appendix += body
            appendix += ["```", ""]
        stats["窗口数"] = len(windows)
        stats["窗口覆盖行数"] = sum(e - s + 1 for _, s, e in windows)

    summary = [
        "## 装配摘要",
        "",
        f"- 条目 {stats['条目数']} 条；OCR {stats['OCR 行数']} 行",
        f"- 引文 {stats['引文总数']} 条：✅ 可溯源 {stats['引文可溯源']}，❌ 未找到 {stats['引文未找到']}",
    ]
    if stats["无 evidence 的条目"]:
        summary.append(f"- ⚠️ 无 evidence 的条目：{stats['无 evidence 的条目'][:15]}")
    if stats["锚点缺失的条目"]:
        summary.append(f"- ⚠️ 标准包无可用锚点（无法机械检索，须人工判断该查什么）：{stats['锚点缺失的条目'][:15]}")
    if stats["引文未找到"]:
        summary.append("- ⛔ 「❌ OCR 中未找到」是**阻断级线索**：引文要么来自别的患者、要么是编造。逐条核实后按 `judgment-repair.md` 改判。")
    summary.append("")

    text = "\n".join(head + summary) + "\n" + "\n".join(blocks) + "\n" + "\n".join(appendix)
    if len(text) > MAX_BUNDLE_CHARS:
        # 超限时砍窗口附录、保条目块：判定 + 引文核验是本产物不可替代的部分，
        # 而窗口随时可以按条目块里给出的行号补读。
        #
        # ⚠️ 提示必须放在**摘要里**（文件开头），不能追加到末尾 —— 追加在末尾时它自己会被
        # 截断掉，QC 只看到一份没有窗口、也没说为什么的产物（单测抓到过这一版）。
        notice = "- ⚠️ 产物超长：**OCR 窗口附录已省略**。每个条目块里都给了命中行号，需要原文时按行号 `read_file` 补读该段。"
        text = "\n".join(head + summary[:-1] + [notice, ""]) + "\n" + "\n".join(blocks)
        text = _truncate(text, MAX_BUNDLE_CHARS)
        stats["超长已省略窗口"] = True
    return text, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="装配 QC 取证素材包（把逐条取证压成一次读）")
    ap.add_argument("--criteria", required=True, help="本轨标准包 criteria_judge_{TRACK}.json")
    ap.add_argument("--judgments", required=True, help="本轨判定 judgments_draft_{id}_{TRACK}.json")
    ap.add_argument("--ocr", action="append", default=[], help="该患者 ocr_records.md（可多次传入）")
    ap.add_argument("--out", required=True, help="输出 evidence_bundle_{id}_{TRACK}.md")
    ap.add_argument("--patient", default="", help="患者ID（写入产物标题）")
    ap.add_argument("--track", default="", choices=["", "IN", "EX"], help="轨（写入产物标题）")
    args = ap.parse_args(argv)

    try:
        criteria = load_json(Path(args.criteria))
        judgments = load_json(Path(args.judgments))
    except FileNotFoundError as e:
        print(f"输入不存在：{e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"JSON 不可解析：{e}", file=sys.stderr)
        return 2

    text, stats = build_bundle(
        criteria,
        judgments,
        [Path(p) for p in args.ocr],
        patient=args.patient,
        track=args.track,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(f"→ {out}  ({len(text):,} 字符)")
    print(f"  条目 {stats['条目数']}　引文 {stats['引文总数']}（✅{stats['引文可溯源']} / ❌{stats['引文未找到']}）")
    if stats["无 evidence 的条目"]:
        print(f"  ⚠️ 无 evidence：{stats['无 evidence 的条目'][:10]}")
    if stats["引文未找到"]:
        print("  ⛔ 存在 OCR 中找不到的引文，QC 须逐条核实（可能跨患者污染或编造）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
