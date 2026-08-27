#!/usr/bin/env python3
"""入排章节定位 + 提取完整性机械自检。

为什么必须用脚本而不是 grep -n / awk NR
--------------------------------------
`read_file(start_line, end_line)` 在 sandbox 里的实现是

    content = "\\n".join(content.splitlines()[start_line - 1 : end_line])

`str.splitlines()` 除 `\\n` 外还在 `\\f`(换页) `\\v` `\\x1c` `\\x1d` `\\x1e` `\\x85`
`\\u2028` `\\u2029` 处断行，而 `grep -n` / `awk NR` 只认 `\\n`。PDF 转出的方案 .md
往往每页一个 `\\f`，两套行号于是整体错位。

历史故障 thread `ec24d087`：试验方案.md 有 131 个 `\\f`，其中 65 个在排除标准之前。
主代理用 `grep -n` 得到「排除段 3820-3988」，`read_file` 按 splitlines 坐标去读，
实际落在原文 3755-3923，只读到排除第 1..11 条（真实 20 条）。更糟的是它的自检脚本
也用 `splitlines()` 数源末条号，同样得 11 —— **两个错误在同一坐标系里互相抵消**，
`n == N` 成立，自检空过，9 条排除标准静默丢失，研究周期等补充章节也一并漏掉。

因此本脚本：
1. 统一用 `splitlines()` 坐标系（与 `read_file` 一致），落盘的 `段行号` 可直接喂 read_file；
2. 显式报告文件里的异常断行符数量与两套坐标系的偏移量，便于一眼看出错位风险；
3. 源末条号由脚本从**源文件**独立算出，不再由主代理从自己的提取结果里数 —— 这是
   `--verify-raw` 能真正发现丢条的前提（否则永远是循环论证）。

用法
----
    # ① 定位：写入 criteria_meta.json 的 段行号 / 末条号 / 补充章节
    locate_criteria_sections.py --protocol <方案.md> --workspace <ws> [--json]

    # ② 自检：拿 ① 的源基线核对提取结果，不通过 exit 2
    locate_criteria_sections.py --protocol <方案.md> --workspace <ws> \\
        --verify-raw <ws>/eligibility_criteria_raw.md [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# `str.splitlines()` 认这些为行边界，`grep -n` / `awk NR` 不认
EXOTIC_BREAKS = {
    "\x0b": "\\v 垂直制表",
    "\x0c": "\\f 换页",
    "\x1c": "\\x1c 文件分隔",
    "\x1d": "\\x1d 组分隔",
    "\x1e": "\\x1e 记录分隔",
    "\x85": "\\x85 NEL",
    "\u2028": "U+2028 行分隔",
    "\u2029": "U+2029 段分隔",
}

# 条目行：编号 + `.`/`、` + 空白（`5.  首次给药前…`）
ITEM_RE = re.compile(r"^[ \t]{0,3}#{0,6}[ \t]*(\d{1,2})[.．、](?!\d)")
# 章节标题行：`5 药物与治疗` / `4.3 研究终点` / `5.1.1 药品信息`（编号后直接跟空白）
HEADING_RE = re.compile(r"^[ \t]{0,3}(\d{1,2}(?:\.\d{1,2})*)[ \t]+(\S.*)$")
# Markdown 标题行
MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S")
# Markdown 编号标题行（`###### 4.1入选标准` / `#### 5药物与治疗`）；条目标题（`#### 7．…`）由 ITEM_RE 先排除
MD_NUM_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(\d{1,2}(?:\.\d{1,2})*)([^\d\s．、。，,：:；;].*)$")
# 目录行（点串导航）
TOC_RE = re.compile(r"\.{4,}")

IN_TITLE_RE = re.compile(r"入\s*选\s*标\s*准")
EX_TITLE_RE = re.compile(r"排\s*除\s*标\s*准")

# 与入排判定相关的补充章节（源文件里存在才要求提取，见 criteria-extraction.md ③.4）
SUPPLEMENT_KEYWORDS = (
    "研究周期",
    "研究设计",
    "试验设计",
    "访视",
    "筛选期",
    "合并用药",
    "禁用药",
    "附录",
)


class LocateBlocked(Exception):
    """机械闸阻断。"""


def _norm(text: str) -> str:
    """全角/半角与空白归一，用于标题包含性比对。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def scan_breaks(raw: str) -> dict:
    """统计异常断行符，给出两套坐标系的行数差。

    `split("\\n")` 对以换行结尾的文本会多出一个空尾元素，而 `splitlines()` 不会；
    不抹平这个差异，偏移量会被凭空 -1，恰好抵消掉一个 `\\f` 的增量。
    """
    found = {desc: raw.count(ch) for ch, desc in EXOTIC_BREAKS.items() if ch in raw}
    n_lines = len(raw.split("\n")) - (1 if raw.endswith("\n") else 0)  # == grep -n 行数
    s_lines = len(raw.splitlines())
    return {
        "异常断行符": found,
        "split_n_行数": n_lines,
        "splitlines_行数": s_lines,
        "坐标系偏移": s_lines - n_lines,
    }


def last_contiguous(nums: list[int]) -> int:
    """从 1 开始的最长连续前缀长度。

    对重复编号（跨页重复、子项误命中）和尾部杂散编号都稳健：
    [1,2,3,3,4,9] → 4；[2,3] → 0。
    """
    expected = 1
    for n in nums:
        if n == expected:
            expected += 1
    return expected - 1


def item_numbers(lines: list[str]) -> list[int]:
    return [int(m.group(1)) for line in lines if (m := ITEM_RE.match(line))]


def _headings(lines: list[str]) -> list[tuple[int, tuple[int, ...], str]]:
    """返回 [(0-based 行号, 编号元组, 标题文本)]，跳过目录行。"""
    out = []
    for i, line in enumerate(lines):
        if TOC_RE.search(line):
            continue
        if ITEM_RE.match(line):
            continue
        m = HEADING_RE.match(line)
        if m:
            nums = tuple(int(x) for x in m.group(1).split("."))
            out.append((i, nums, m.group(2).strip()))
            continue
        md = MD_NUM_HEADING_RE.match(line)
        if md:
            nums = tuple(int(x) for x in md.group(1).split("."))
            out.append((i, nums, md.group(2).strip()))
    return out


def locate(protocol: Path) -> dict:
    """定位入选/排除章节边界与源末条号（splitlines 坐标系，1-based 半开区间）。"""
    raw = protocol.read_text(encoding="utf-8")
    lines = raw.splitlines()
    breaks = scan_breaks(raw)
    heads = _headings(lines)

    in_head = next((h for h in heads if IN_TITLE_RE.search(h[2])), None)
    ex_head = next((h for h in heads if EX_TITLE_RE.search(h[2])), None)
    if in_head is None or ex_head is None:
        missing = []
        if in_head is None:
            missing.append("入选标准")
        if ex_head is None:
            missing.append("排除标准")
        raise LocateBlocked(f"⛔ 未能在 {protocol.name} 正文中定位章节标题：{'、'.join(missing)}")
    if ex_head[0] <= in_head[0]:
        raise LocateBlocked(f"⛔ 排除标准标题（行 {ex_head[0] + 1}）不在入选标准（行 {in_head[0] + 1}）之后")

    # 终点 = 排除标准之后第一个编号更大的章节标题（如 4.3 / 5 / 5.1）
    ex_num = ex_head[1]
    term = next((h for h in heads if h[0] > ex_head[0] and h[1] > ex_num), None)
    end = (term[0] if term else len(lines)) + 1  # 1-based 半开区间上界

    in_start, ex_start = in_head[0] + 1, ex_head[0] + 1
    in_nums = item_numbers(lines[in_start - 1 : ex_start - 1])
    ex_nums = item_numbers(lines[ex_start - 1 : end - 1])

    supplements = [{"行号": h[0] + 1, "编号": ".".join(str(x) for x in h[1]), "标题": h[2]} for h in heads if any(k in h[2] for k in SUPPLEMENT_KEYWORDS)]

    return {
        "坐标系": "splitlines（与 read_file 一致）",
        "断行符扫描": breaks,
        "段行号": {
            "入选": {"start": in_start, "end": ex_start},
            "排除": {"start": ex_start, "end": end},
        },
        "章节标题": {
            "入选": f"{'.'.join(str(x) for x in in_head[1])} {in_head[2]}",
            "排除": f"{'.'.join(str(x) for x in ex_head[1])} {ex_head[2]}",
            "终点": (f"{'.'.join(str(x) for x in term[1])} {term[2]}" if term else "(文件末尾)"),
        },
        "末条号": {"入选": last_contiguous(in_nums), "排除": last_contiguous(ex_nums)},
        "原始编号序列": {"入选": in_nums, "排除": ex_nums},
        "补充章节": supplements,
    }


def _is_heading_line(line: str) -> bool:
    """标题行 = Markdown `#` 标题 或 `4.1 入选标准` 式编号标题；正文提及不算。

    `- 来源：试验方案.pdf 第4章（4.1 入选标准 / 4.2 排除标准）` 这类行同时提到两个标题，
    若当成标题会让分段错位（thread `ec24d087` 自检报错即此）。
    """
    if TOC_RE.search(line):
        return False
    if ITEM_RE.match(line):
        return False
    return bool(HEADING_RE.match(line) or MD_NUM_HEADING_RE.match(line))


def split_raw(raw_md: str) -> tuple[list[str], list[str]]:
    """把 eligibility_criteria_raw.md 切成入选段 / 排除段（只认标题行）。"""
    lines = raw_md.splitlines()
    heads = [i for i, ln in enumerate(lines) if _is_heading_line(ln)]
    ex_idx = next((i for i in heads if EX_TITLE_RE.search(lines[i])), None)
    in_idx = next(
        (i for i in heads if IN_TITLE_RE.search(lines[i]) and (ex_idx is None or i < ex_idx)),
        None,
    )
    if in_idx is None or ex_idx is None:
        raise LocateBlocked("⛔ eligibility_criteria_raw.md 里找不到「入选标准」/「排除标准」标题，无法分段自检")
    return lines[in_idx + 1 : ex_idx], lines[ex_idx + 1 :]


def raw_section_lines(raw_md: str) -> dict:
    """`eligibility_criteria_raw.md` **自身**的入选/排除段行号（1-based 半开区间）。

    ⛔ 与 `段行号` 是两套不同坐标：`段行号` 属 `uploads/试验方案.md`（数千行），
    `raw段行号` 属 raw.md（数百行）。把前者喂给 `read_file` 读 raw.md 会越界，
    而 `read_file` 对越界切片返回**静默空字符串**（`if not content` 的兜底在切片之前），
    子代理拿到空输入却不会报错 —— thread `6e5ac7c1` 由此凭空编造了 92% 的条目。
    """
    lines = raw_md.splitlines()
    heads = [i for i, ln in enumerate(lines) if _is_heading_line(ln)]
    ex_idx = next((i for i in heads if EX_TITLE_RE.search(lines[i])), None)
    in_idx = next(
        (i for i in heads if IN_TITLE_RE.search(lines[i]) and (ex_idx is None or i < ex_idx)),
        None,
    )
    if in_idx is None or ex_idx is None:
        return {}
    # 排除段终点 = 排除标题之后的下一个同级/更高级标题，否则文件末尾
    ex_end = next((i for i in heads if i > ex_idx and not EX_TITLE_RE.search(lines[i])), len(lines))
    return {
        "入选": {"start": in_idx + 1, "end": ex_idx + 1},
        "排除": {"start": ex_idx + 1, "end": ex_end + 1},
    }


def verify_raw(loc: dict, raw_path: Path) -> list[str]:
    """用源基线核对提取结果，返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    if not raw_path.exists():
        return [f"⛔ 提取结果不存在：{raw_path}"]
    text = raw_path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"⛔ 提取结果为空文件：{raw_path}"]
    loc["raw段行号"] = raw_section_lines(text)
    loc["raw总行数"] = len(text.splitlines())

    in_lines, ex_lines = split_raw(text)
    for track, seg, src_max in (
        ("入选", in_lines, loc["末条号"]["入选"]),
        ("排除", ex_lines, loc["末条号"]["排除"]),
    ):
        nums = item_numbers(seg)
        got = last_contiguous(nums)
        if got != src_max:
            missing = [n for n in range(1, src_max + 1) if n not in set(nums)]
            problems.append(
                f"⛔ {track}标准丢条：源文件声明 1..{src_max}，提取结果只到 1..{got}（缺 {missing or '（编号不连续）'}）；源区间 {loc['段行号'][track]['start']}-{loc['段行号'][track]['end']}（splitlines 坐标，可直接喂 read_file）"
            )

    # 源文件里存在的补充章节，提取结果必须体现
    body = _norm(text)
    absent = [s for s in loc["补充章节"] if _norm(s["标题"]) not in body]
    if absent:
        detail = "、".join(f"{s['编号']} {s['标题']}（源行 {s['行号']}）" for s in absent)
        problems.append(f"⛔ 源文件存在但提取结果未包含的入排相关补充章节：{detail}")
    return problems


def merge_meta(workspace: Path, loc: dict) -> Path:
    """把段行号/末条号/补充章节并入 criteria_meta.json，保留既有字段。"""
    meta_path = workspace / "criteria_meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    meta["段行号"] = loc["段行号"]
    meta["段行号归属文件"] = "uploads/试验方案.md（⛔ 不可用于 read_file eligibility_criteria_raw.md）"
    meta["末条号"] = loc["末条号"]
    meta["补充章节"] = loc["补充章节"]
    meta["行号坐标系"] = loc["坐标系"]
    if loc.get("raw段行号"):
        meta["raw段行号"] = loc["raw段行号"]
        meta["raw总行数"] = loc["raw总行数"]
        meta["raw段行号归属文件"] = "workspace/eligibility_criteria_raw.md（双轨解析用这一套）"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta_path


def summarize(loc: dict, problems: list[str], meta_path: Path | None) -> str:
    out = [f"坐标系：{loc['坐标系']}"]
    br = loc["断行符扫描"]
    if br["异常断行符"]:
        detail = "、".join(f"{d}×{n}" for d, n in br["异常断行符"].items())
        out.append(f"⚠️ 检出异常断行符：{detail} → splitlines 比 grep -n 多 {br['坐标系偏移']} 行；**禁止**把 grep -n / awk NR 的行号喂给 read_file，用下方段行号")
    for track in ("入选", "排除"):
        seg = loc["段行号"][track]
        out.append(f"{track}：行 {seg['start']}-{seg['end']}，源末条号 {loc['末条号'][track]}")
    out.append(f"章节：{loc['章节标题']['入选']} / {loc['章节标题']['排除']} → 终点 {loc['章节标题']['终点']}")
    if loc["补充章节"]:
        out.append("补充章节（须一并提取）：" + "、".join(f"{s['编号']} {s['标题']}" for s in loc["补充章节"]))
    if loc.get("raw段行号"):
        r = loc["raw段行号"]
        out.append(f"raw段行号（raw.md 共 {loc['raw总行数']} 行，**双轨解析用这一套**）：入选 {r['入选']['start']}-{r['入选']['end']}、排除 {r['排除']['start']}-{r['排除']['end']}")
        out.append("⛔ 上方 `段行号` 属试验方案.md，喂给 read_file 读 raw.md 会越界并静默返回空串")
    if meta_path:
        out.append(f"已写入 {meta_path}")
    out.extend(problems if problems else [])
    out.append("⛔ 自检未通过" if problems else "✅ 自检通过")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="入排章节定位 + 提取完整性机械自检")
    ap.add_argument("--protocol", required=True, type=Path, help="试验方案 .md（sidecar）")
    ap.add_argument("--workspace", type=Path, help="写入 criteria_meta.json 的工作区")
    ap.add_argument("--verify-raw", type=Path, help="自检 eligibility_criteria_raw.md")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not args.protocol.exists():
        print(f"⛔ 方案文件不存在：{args.protocol}", file=sys.stderr)
        return 2
    try:
        loc = locate(args.protocol)
        problems = verify_raw(loc, args.verify_raw) if args.verify_raw else []
    except LocateBlocked as exc:
        if args.json:
            print(json.dumps({"problems": [str(exc)]}, ensure_ascii=False, indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    meta_path = merge_meta(args.workspace, loc) if args.workspace else None
    if args.json:
        print(json.dumps({**loc, "problems": problems}, ensure_ascii=False, indent=2))
    else:
        print(summarize(loc, problems, meta_path))
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
