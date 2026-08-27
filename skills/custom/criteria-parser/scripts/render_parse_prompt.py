#!/usr/bin/env python3
"""把解析委派模板机械渲染成 prompt 文件(解析/重做/QC/修订)。

为什么必须脚本渲染(会话 881e7ba8,2026-08-26)
------------------------------------------
1. **主代理手写 19 份委派 ~19k 字符**:轮次号、闸 total、点名条目全靠手填,填错即误导
   子代理;每份都占 lead 输出 token。
2. **每个子代理被要求自读 34KB parsing-rules.md**:EX 重做任务前 4 步 ~200k token
   花在学规则,写产物时上下文耗尽 → 27 个实体只精拆 2 个、17 条「待人工判定」占位符。
   本脚本把任务所需规则节(拆分原则/条件转化规则/可获取性判定标准)按节标题**从
   parsing-rules.md 机械抽取后内嵌**——规则单一权威不变,子代理零自读全文。
3. **手写变体即漂移点**(判定域同款故障 9a93ccc9):重做 prompt 没继承「禁 rm/
   结构闸自修」纪律 → rm 销毁拆分版。渲染保证模板逐字到达。

用法
----
    # 初解析:一次渲染两轨
    render_parse_prompt.py --workspace <ws>
    # 整轨重做(结构闸未过后;点名来自 criteria_structure_gate_{TRACK}.json)
    render_parse_prompt.py --workspace <ws> --kind redo --track EX
    # 语义 QC / 修订(每轮一次)
    render_parse_prompt.py --workspace <ws> --kind qc --track IN --round 2
    render_parse_prompt.py --workspace <ws> --kind repair --track IN --round 2

stdout 给出每份 prompt 的路径与 `task(prompt_file=…)` 派发行,主代理照抄即可。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REFS = SCRIPTS_DIR.parent / "references"
DEFAULT_DELEGATION = REFS / "parse-delegation.md"
DEFAULT_PARSING_RULES = REFS / "parsing-rules.md"
DEFAULT_QC_CHECKLIST = REFS / "criteria-qc-checklist.md"
DEFAULT_REPAIR = REFS / "criteria-repair.md"
USER_DATA = "/mnt/user-data"

TRACK_CN = {"IN": "入选标准", "EX": "排除标准"}  # 轨名(子代理可读)
TRACK_KEY = {"IN": "入选", "EX": "排除"}  # meta 的 raw段行号/末条号 键(短键,与模板占位符一致)

# 内嵌进解析/重做 prompt 的规则节(按 parsing-rules.md 的 `## ` 标题精确匹配)
RULE_SECTION_TITLES = ("拆分原则（最小子颗粒度）", "条件转化规则", "可获取性判定标准")

# 白名单占位符:渲染后不得残留任何一个
LEFTOVER_RE = re.compile(
    r"\{(?:raw段行号(?:\.[^{}]+)?|末条号\.[^{}]+|PARSING_RULES|GATE_PROBLEMS|PREV_REPORT_BLOCK|TRACK|轨名|分片名|ROUND|round|GATE_TOTAL|total|start|end)\}"
)

# 模板锚点:内容锚点而非块序号(块位置随文档漂移;锚点缺失 = 模板被改,显式失败)。
# 模板源:parse/redo → parse-delegation.md(节提取,内嵌 ```json 示例);qc/repair → 各自
# 唯一权威文档的围栏块(单层围栏,非嵌套)。
TEMPLATE_SOURCES = {
    "parse_IN": (DEFAULT_DELEGATION, "把**入选标准**解析为结构化 JSON"),
    "parse_EX": (DEFAULT_DELEGATION, "把**排除标准**解析为结构化 JSON"),
    "redo": (DEFAULT_DELEGATION, "重做{轨名}整轨解析"),
    "qc": (DEFAULT_QC_CHECKLIST, "做第 {round} 轮 criteria 语义 QC"),
    "repair": (DEFAULT_REPAIR, "修订**{分片名}标准**的结构化 JSON"),
}

PREV_REPORT_BLOCK = (
    "- /mnt/user-data/workspace/criteria_qc_{TRACK}.json（上一轮报告；第 2 轮起）——\n"
    "  **仅用于给已修条目标 `status: \"fixed\"`**，⛔ 不得用它缩小本轮复核范围"
)

# 指纹:渲染产物必须包含的承重内容(防模板被删段后静默渲染半个模板)
FINGERPRINTS = {
    "parse": ("check_track_structure.py",),
    "redo": ("check_track_structure.py",),
    "repair": ("check_track_structure.py",),
    "qc": ("criteria_qc_bundle_", "criteria-qc-checklist.md"),
}


class RenderBlocked(Exception):
    """模板/输入不合法,渲染必须显式失败——静默渲染半个模板比失败危险得多。"""


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RenderBlocked(f"⛔ 读取失败:{path}:{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RenderBlocked(f"⛔ JSON 解析失败:{path}:{exc}") from exc
    return data if isinstance(data, dict) else {}


def extract_templates(markdown: str) -> dict[str, str]:
    """按「## 模板」节抽取模板正文,剥掉围栏标记行。

    模板节内嵌 ```json 示例块(文档里是四段交替围栏),按围栏配对扫描会在嵌套处截断;
    节提取(标题到下一个 `## `)对文档增删段稳健,json 示例作为纯文本进入渲染产物。"""
    templates: dict[str, str] = {}
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current is not None:
                _classify("\n".join(current), templates)
            current = [] if line.startswith("## 模板") else None
        elif current is not None:
            if not line.startswith("```"):
                current.append(line)
    if current is not None:
        _classify("\n".join(current), templates)
    return templates


def _classify(block: str, templates: dict[str, str]) -> None:
    for name, (_src, anchor) in TEMPLATE_SOURCES.items():
        if name not in ("parse_IN", "parse_EX", "redo"):
            continue
        if anchor in block:
            if name in templates:
                raise RenderBlocked(f"parse-delegation.md 里有多个模板都含锚点「{anchor}」,无法判定用哪一个")
            templates[name] = block.strip() + "\n"


def extract_fenced_block(markdown: str, anchor: str, source_name: str) -> str:
    """qc/repair 模板:单层围栏块 + 内容锚点(qc-checklist/repair 的模板无嵌套围栏)。"""
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", markdown, re.S | re.M)
    hit = [b for b in blocks if anchor in b]
    if not hit:
        raise RenderBlocked(f"{source_name} 找不到委派模板块(锚点:「{anchor}」)。模板结构可能被改动,请先核对。")
    if len(hit) > 1:
        raise RenderBlocked(f"{source_name} 有 {len(hit)} 个块都含锚点「{anchor}」,无法判定用哪一个。请保持唯一。")
    return hit[0].strip() + "\n"


def extract_rule_sections(parsing_rules_md: str) -> str:
    """从 parsing-rules.md 按 `## ` 标题切节,抽取 RULE_SECTION_TITLES 指定的节全文。"""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in parsing_rules_md.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    picked: list[str] = []
    for title, body in sections:
        if title in RULE_SECTION_TITLES:
            picked.append(f"## {title}\n" + "\n".join(body).rstrip())
    missing = [t for t in RULE_SECTION_TITLES if t not in {s[0] for s in sections}]
    if missing:
        raise RenderBlocked(f"parsing-rules.md 缺节:{missing}——标题可能被改动,请核对节名")
    if len(picked) != len(RULE_SECTION_TITLES):
        raise RenderBlocked(f"规则节抽取不完整:{len(picked)}/{len(RULE_SECTION_TITLES)}")
    return "\n\n".join(picked) + "\n"


def gate_problems_block(ws: Path, track: str) -> str:
    gate = load_json(ws / f"criteria_structure_gate_{track}.json")
    problems = gate.get("problems")
    if gate.get("exit_code") != 2 or not isinstance(problems, list) or not problems:
        raise RenderBlocked(
            f"⛔ 重做依据缺失:criteria_structure_gate_{track}.json 需为 exit_code=2 且带非空 problems"
            "——先跑结构闸再派重做"
        )
    return "\n".join(f"- {p}" for p in problems)


def render(ws: Path, kind: str, track: str | None, round_no: int | None) -> list[tuple[str, str]]:
    """渲染 (文件名, 正文) 列表。占位符只按白名单精确替换。"""
    delegation = DEFAULT_DELEGATION.read_text(encoding="utf-8")
    templates = extract_templates(delegation)
    rules = extract_rule_sections(DEFAULT_PARSING_RULES.read_text(encoding="utf-8"))
    meta = load_json(ws / "criteria_meta.json")
    raw_lines = meta.get("raw段行号") or {}
    last_nums = meta.get("末条号") or {}
    if kind in ("parse", "redo", "repair"):
        for key in ("入选", "排除"):
            needed = kind == "parse" or key == TRACK_KEY.get(track)
            if needed:
                seg = raw_lines.get(key) or {}
                if not (isinstance(seg.get("start"), int) and isinstance(seg.get("end"), int)):
                    raise RenderBlocked(
                        f"⛔ meta 缺 `raw段行号.{key}`(需 start/end 整数)——先跑 locate_criteria_sections.py ① 与章节提取"
                    )

    outputs: list[tuple[str, str]] = []

    def fill(track_key: str, template: str, *, extra: dict[str, str] | None = None, kind_key: str = kind) -> str:
        cn = TRACK_CN[track_key]
        key = TRACK_KEY[track_key]
        seg = raw_lines.get(key, {})
        subs = {
            "{PARSING_RULES}": rules,
            "{轨名}": cn,
            "{分片名}": key,
            "{TRACK}": track_key,
            "{start}": str(seg.get("start", "")),
            "{end}": str(seg.get("end", "")),
            f"{{raw段行号.{key}.start}}": str(seg.get("start", "")),
            f"{{raw段行号.{key}.end}}": str(seg.get("end", "")),
            f"{{末条号.{key}}}": str(last_nums.get(key, "")),
            "{raw段行号.start}": str(seg.get("start", "")),
            "{raw段行号.end}": str(seg.get("end", "")),
            "{ROUND}": str(round_no if round_no is not None else 1),
            "{round}": str(round_no if round_no is not None else 1),
        }
        if extra:
            subs.update(extra)
        text = template
        for old, new in subs.items():
            text = text.replace(old, new)
        leftover = sorted(set(LEFTOVER_RE.findall(text)))
        if leftover:
            raise RenderBlocked(f"⛔ 模板渲染后残留未替换占位符:{leftover}——模板与脚本占位符白名单漂移,请核对")
        for fp in FINGERPRINTS.get(kind_key, ()):
            if fp not in text:
                raise RenderBlocked(f"⛔ 渲染产物缺指纹「{fp}」——模板可能被删段,请核对模板源文档")
        return text

    if kind == "parse":
        for track_key, name in (("IN", "parse_IN"), ("EX", "parse_EX")):
            if name not in templates:
                raise RenderBlocked(f"parse-delegation.md 找不到 {name} 模板(锚点:{TEMPLATE_SOURCES[name][1]})")
            outputs.append((f"{name}.md", fill(track_key, templates[name])))
    elif kind == "redo":
        if track not in TRACK_CN:
            raise RenderBlocked(f"--track 必须是 IN/EX,收到 {track!r}")
        if "redo" not in templates:
            raise RenderBlocked("parse-delegation.md 找不到重做模板(模板③)")
        problems = gate_problems_block(ws, track)
        outputs.append((f"redo_{track}.md", fill(track, templates["redo"], extra={"{GATE_PROBLEMS}": problems})))
    elif kind in ("qc", "repair"):
        if track not in TRACK_CN:
            raise RenderBlocked(f"--track 必须是 IN/EX,收到 {track!r}")
        if round_no is None or round_no < 1:
            raise RenderBlocked(f"--kind {kind} 需要 --round(≥1)")
        src_path, anchor = TEMPLATE_SOURCES[kind]
        template = extract_fenced_block(src_path.read_text(encoding="utf-8"), anchor, src_path.name)
        # qc 前置:结构闸必须已过(模板要求子代理自检 gate 文件;渲染侧同校验,
        # 防止「结构闸没跑/没过就派 QC」白烧一轮 —— thread 345f2bf4)
        gate = load_json(ws / f"criteria_structure_gate_{track}.json")
        gate_total = gate.get("total")
        if kind == "qc" and gate.get("exit_code") != 0:
            raise RenderBlocked(
                f"⛔ criteria_structure_gate_{track}.json 需为 exit_code=0 才能派 QC——先跑结构闸"
            )
        prev_block = PREV_REPORT_BLOCK.replace("{TRACK}", track) if round_no >= 2 else ""
        outputs.append((
            f"{kind}_{track}_r{round_no}.md",
            fill(track, template, extra={
                "{PREV_REPORT_BLOCK}": prev_block,
                "{total}": str(gate_total if gate_total is not None else ""),
            }),
        ))
    else:
        raise RenderBlocked(f"未知 --kind {kind!r}")
    return outputs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="机械渲染解析委派 prompt(解析/重做/QC/修订)")
    ap.add_argument("--workspace", required=True, help="workspace 绝对路径")
    ap.add_argument("--kind", default="parse", choices=["parse", "redo", "qc", "repair"])
    ap.add_argument("--track", default=None, help="redo/qc/repair 必填(IN/EX)")
    ap.add_argument("--round", type=int, default=None, help="qc/repair 的轮次号")
    ap.add_argument("--out-dir", default=None, help="默认 <workspace>/prompts")
    args = ap.parse_args(argv)

    if args.kind in ("redo", "qc", "repair") and not args.track:
        print(f"⛔ --kind {args.kind} 需要 --track", file=sys.stderr)
        return 2

    try:
        rendered = render(Path(args.workspace), args.kind, args.track, args.round)
    except RenderBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else Path(args.workspace) / "prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in rendered:
        (out_dir / name).write_text(text, encoding="utf-8")
        print(f"prompt_file: {out_dir / name}")
        print(f'task(prompt_file="{out_dir / name}")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
