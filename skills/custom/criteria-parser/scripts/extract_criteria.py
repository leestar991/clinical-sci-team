#!/usr/bin/env python3
"""章节原文机械落盘:提取区块 → 切片 → 组装 → 自检。

为什么必须用脚本(会话 bc8a9bc7,2026-08-26)
------------------------------------------
`locate_criteria_sections.py` 已机械给出段行号/末条号/补充章节,但「按行号切原文 →
落盘 `eligibility_criteria_raw.md`」此前由主代理用 `write_file` 逐字重写:11,441 输出
token、215s 生成(墙钟 40%),且产物永久驻留主代理上下文。纯机械的切片复制不需要任何
语义判断,却付了 LLM 生成价。本脚本接管这最后一步。

分工(谁来定什么)
----------------
- locate ①   段行号 / 末条号 / 补充章节(机械定位,splitlines 坐标)
- 主代理     把「提取区块」+「方案元数据」写进 `criteria_meta.json` —— 块边界与命名是
             **语义工作**(补充章节的终点、附录摘哪些),浓缩成几十 token 的清单;
             ⛔ 主代理不得用 `write_file` 抄写章节原文
- 本脚本     按清单切片、组装 `# 方案基本信息` 节与各块、落盘,并复用 locate 的
             `verify_raw` 做源基线核对(丢条 / 缺补充章节 → exit 2,不落盘)

用法
----
    extract_criteria.py --meta <criteria_meta.json> --source <试验方案.md> \
        --out <eligibility_criteria_raw.md> [--json]

meta 的「提取区块」形态(按 raw 顺序):

    [{"标题": "研究设计与周期", "start": 1400, "end": 1655}, ...]

行号是 **splitlines 坐标(1-based 闭区间)**,与 `read_file` 一致,可直接取自 locate ①
的输出;块区间互不重叠(重叠 = 同段原文抄两遍,拒绝)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import locate_criteria_sections as locate  # noqa: E402  复用 verify_raw 与坐标系契约

RECEIPT_KEYS = ("raw段行号", "raw总行数", "raw段行号归属文件")


class ExtractBlocked(Exception):
    """输入不合法(缺块清单 / 行号越界 / 区间重叠),落盘之前就能判定。"""


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExtractBlocked(f"⛔ 读取失败:{path}:{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExtractBlocked(f"⛔ JSON 解析失败:{path}:{exc}") from exc
    if not isinstance(data, dict):
        raise ExtractBlocked(f"⛔ {path.name} 顶层必须是 JSON 对象")
    return data


def validate_blocks(meta: dict, n_source_lines: int) -> list[dict]:
    """块清单存在性 + 区间合法性 + 互不重叠。返回排序检查用的原序列表。"""
    blocks = meta.get("提取区块")
    if not isinstance(blocks, list) or not blocks:
        raise ExtractBlocked(
            "⛔ meta 缺「提取区块」(每块 {标题, start, end},按 raw 顺序)。"
            "块边界与命名是语义工作,由定位阶段写进 meta,本脚本不做行号推导;"
            "先跑 locate_criteria_sections.py 并把补充章节的终点补全成块清单"
        )
    problems: list[str] = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            problems.append(f"块 #{i + 1} 不是对象:{b!r}")
            continue
        title, start, end = b.get("标题"), b.get("start"), b.get("end")
        if not title or not str(title).strip():
            problems.append(f"块 #{i + 1} 缺标题")
        for name, v in (("start", start), ("end", end)):
            if not isinstance(v, int) or isinstance(v, bool):
                problems.append(f"块「{title}」的 {name} 必须是整数,收到 {v!r}")
        if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool):
            if not (1 <= start <= end <= n_source_lines):
                problems.append(
                    f"块「{title}」区间 {start}-{end} 越界(源文件共 {n_source_lines} 行,"
                    "splitlines 坐标;注意 grep -n 与 splitlines 会因换页符错位,行号取自 locate ①)"
                )
    ordered = sorted(
        (b for b in blocks if isinstance(b, dict) and isinstance(b.get("start"), int) and isinstance(b.get("end"), int)),
        key=lambda b: b["start"],
    )
    for prev, cur in zip(ordered, ordered[1:]):
        if cur["start"] <= prev["end"]:
            problems.append(
                f"块「{prev.get('标题')}」({prev['start']}-{prev['end']}) 与块「{cur.get('标题')}」"
                f"({cur['start']}-{cur['end']}) 区间重叠 —— 同段原文会被抄两遍"
            )
    if problems:
        raise ExtractBlocked("⛔ 提取区块不合法:\n  " + "\n  ".join(problems))
    return blocks


def render_metadata(meta: dict) -> list[str]:
    """meta「方案元数据」→ `# 方案基本信息` 节(纯键值渲染,无自由文本)。"""
    md = meta.get("方案元数据")
    if not isinstance(md, dict) or not md:
        return []
    lines = ["# 方案基本信息"]
    for key, value in md.items():
        lines.append(f"## {key}")
        if isinstance(value, list):
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(str(value))
    return lines


def assemble(meta: dict, source_lines: list[str], blocks: list[dict]) -> str:
    """组装 raw:基本信息节(若有)在前,其后逐块 `# 标题` + 逐字切片,节/块间空行。"""
    sections: list[list[str]] = []
    md_lines = render_metadata(meta)
    if md_lines:
        sections.append(md_lines)
    for b in blocks:
        sliced = source_lines[b["start"] - 1 : b["end"]]
        if len(sliced) != b["end"] - b["start"] + 1:
            raise ExtractBlocked(f"⛔ 块「{b.get('标题')}」切片行数 {len(sliced)} ≠ 预期 {b['end'] - b['start'] + 1}")
        sections.append([f"# {b['标题']}", *sliced])
    return "\n\n".join("\n".join(s) for s in sections) + "\n"


def write_receipt(meta_path: Path, loc: dict) -> None:
    """成功后把 raw 段行号回执并回 meta(只碰回执三键,不动 locate ① 的定位字段)。"""
    meta = load_json(meta_path)
    meta["raw段行号"] = loc["raw段行号"]
    meta["raw总行数"] = loc["raw总行数"]
    meta["raw段行号归属文件"] = "workspace/eligibility_criteria_raw.md（双轨解析用这一套）"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 meta 提取区块机械切片落盘 eligibility_criteria_raw.md")
    ap.add_argument("--meta", required=True, help="criteria_meta.json(locate ① 产物 + 提取区块/方案元数据)")
    ap.add_argument("--source", required=True, help="试验方案 .md 源文件")
    ap.add_argument("--out", required=True, help="输出的 eligibility_criteria_raw.md")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    def emit(payload: dict) -> None:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif payload.get("problems"):
            for p in payload["problems"]:
                print(p, file=sys.stderr)
        else:
            print(payload["summary"])

    try:
        meta = load_json(Path(args.meta))
        source_lines = Path(args.source).read_text(encoding="utf-8").splitlines()
        blocks = validate_blocks(meta, len(source_lines))
    except (ExtractBlocked, OSError) as exc:
        emit({"problems": [str(exc)]})
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(assemble(meta, source_lines, blocks), encoding="utf-8")

    # 源基线核对复用 locate ② 的 verify_raw:丢条 / 缺补充章节 / 无法分段都在这里拦
    loc = {k: meta[k] for k in ("段行号", "末条号", "补充章节") if k in meta}
    problems: list[str] = []
    try:
        if {"段行号", "末条号", "补充章节"} - set(loc):
            problems.append(
                "⛔ meta 缺 " + str(sorted({"段行号", "末条号", "补充章节"} - set(loc)))
                + " —— 先跑 locate_criteria_sections.py ① 再提取"
            )
        else:
            problems = locate.verify_raw(loc, out_path)
    except locate.LocateBlocked as exc:
        problems = [str(exc)]
    if problems:
        out_path.unlink(missing_ok=True)  # 不留半成品
        emit({"problems": problems})
        return 2

    write_receipt(Path(args.meta), loc)
    emit({
        "summary": (
            f"✅ {out_path} 共 {loc['raw总行数']} 行(入选 {loc['raw段行号']['入选']['start']}-"
            f"{loc['raw段行号']['入选']['end']} / 排除 {loc['raw段行号']['排除']['start']}-"
            f"{loc['raw段行号']['排除']['end']});回执已写入 {args.meta}"
        ),
        "blocks": [{"标题": b["标题"], "行数": b["end"] - b["start"] + 1} for b in blocks],
        "raw总行数": loc["raw总行数"],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
