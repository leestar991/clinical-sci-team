#!/usr/bin/env python3
"""OCR 覆盖率校验 + 重复解析自检（路线 A/B 通用）。

把覆盖率算法从编排层的内联 python 收敛进技能，避免每次重写导致分母算错
（历史故障：用 manifest `total_pages` 当分母，mixed 型 PDF 的文本层页没有图片也
不需要 OCR，于是永远判不覆盖、白跑补漏轮次）。

判定口径：
- 路线 **A**（整份解析）：该 source 只需一份 `ocr/{source}/{source}_full.md`。
- 路线 **B**（逐页解析）：分母 = manifest 中**全部页**（`scanned` + `text`），每页都必须有
  `ocr/{source}/{stem}.md`；但两类页的补法完全不同：
  - `type == "scanned"` → 派 OCR 子代理逐页解析；
  - `type == "text"` → PDF 自带文本层，`pdf_to_image.py` 已导出 `.txt`，
    用 **`collect_text_pages.py`** 机械归集成 `.md`，**禁止派 OCR**（图片都没渲染，
    派了只会触发 `view_image` 兜底、白烧轮次）。

⛔ 分母**不是**只算 `scanned`：文本层页「不需要 OCR」≠「不需要进证据库」。
历史故障 thread `69612125`：`筛选期检查.pdf` 26 页里 11 页是文本层（`page_016..026.txt`，
共 32 KB 真实内容，含 `KRAS ... p.(G13D) 26.29%` 基因检测报告），因分母只算 15 个 scanned 页，
本脚本报 `need=15 done=15 covered=True ✅ 覆盖完整`，11 页证据静默丢失。
后果：`IN-4-1` 被判「无法判断：缺基因检测报告及RAS突变具体结果；需补充分子检测报告」——
判定层准确指出了缺口，而缺的正是被本脚本判为「已覆盖」的那 11 页。
（更早一次相反方向的故障：用 `total_pages` 当分母 → 文本层页永远判不覆盖、白跑补漏轮次。
两次教训合起来才是正确口径：**全部页进分母，按页型分派不同补法**。）
- **重复解析自检**：`parsed/` 下的解析产物目录数应与 OCR 产出数基本相等
  （A：每 source 1 次；B：每页 1 次）。明显偏多说明存在 A/B 双跑、整份+逐页重复、
  或解析了不该解析的文件（如已提取完成的方案文档）。

用法：
    python3 ocr_coverage.py --workspace /mnt/user-data/workspace [--json out.json]

始终 exit 0；结论以 stdout 摘要（与可选 JSON）为准：
`covered=False` 表示还有缺口，`duplicate_parse_suspected=True` 表示疑似重复解析。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

IMG_EXT = (".png", ".jpg", ".jpeg")


def _routes(workspace: Path) -> dict[str, str | None]:
    """从 pdf_classification.json 取 {source_name: ocr_route}。

    `ocr_route` 为空 → 返回 ``None``（"待用户选择处理模式"），**绝不擅自当作 A**：
    默认值会让"未确认"看起来像"已确认"（历史故障）。
    """
    cls_path = workspace / "pdf_classification.json"
    if cls_path.exists():
        try:
            cls = json.loads(cls_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cls = {}
        routes: dict[str, str | None] = {}
        for item in (cls.get("scan") or []) + (cls.get("mixed") or []):
            if isinstance(item, dict) and item.get("source_name"):
                routes[item["source_name"]] = item.get("ocr_route") or None
        if routes:
            return routes
    images = workspace / "images"
    if images.exists():
        return {d.name: None for d in sorted(images.iterdir()) if d.is_dir()}
    return {}


def _scanned_stems(workspace: Path, source: str) -> set[str]:
    img_dir = workspace / "images" / source
    mp = img_dir / f"{source}_manifest.json"
    if mp.exists():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            return {Path(p["filename"]).stem for p in m.get("pages", []) if p.get("type") == "scanned"}
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    if not img_dir.exists():
        return set()
    return {p.stem for p in img_dir.iterdir() if p.suffix.lower() in IMG_EXT}


def _text_stems(workspace: Path, source: str) -> set[str]:
    """manifest 中 `type == "text"` 的页 stem（PDF 自带文本层，已导出 `.txt`）。

    这些页不需要 OCR，但**必须**归集进 `ocr/` 才算证据齐全 —— 见文件头 `69612125`。
    没有 manifest 时退化为扫目录里的 `.txt`。
    """
    img_dir = workspace / "images" / source
    mp = img_dir / f"{source}_manifest.json"
    if mp.exists():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            return {Path(p["filename"]).stem for p in m.get("pages", []) if p.get("type") == "text"}
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    if not img_dir.exists():
        return set()
    return {p.stem for p in img_dir.iterdir() if p.suffix.lower() == ".txt"}


def _ocr_stems(workspace: Path, source: str) -> set[str]:
    ocr_dir = workspace / "ocr" / source
    if not ocr_dir.exists():
        return set()
    return {p.stem for p in ocr_dir.glob("*.md") if p.name != "ocr_records.md"}


def check(workspace: Path) -> dict:
    sources = []
    for source, route in sorted(_routes(workspace).items(), key=lambda kv: kv[0]):
        if route is None:
            sources.append(
                {
                    "source": source,
                    "route": None,
                    "covered": False,
                    "missing": ["<ocr_route 未选择：需用户先确认处理模式>"],
                }
            )
            continue
        if route == "A":
            full = workspace / "ocr" / source / f"{source}_full.md"
            sources.append(
                {
                    "source": source,
                    "route": "A",
                    "covered": full.exists(),
                    "expected": [full.name],
                    "missing": [] if full.exists() else [full.name],
                }
            )
            continue
        scanned = _scanned_stems(workspace, source)
        text = _text_stems(workspace, source)
        need = scanned | text
        done = _ocr_stems(workspace, source)
        missing_scanned = sorted(scanned - done)
        missing_text = sorted(text - done)
        missing = sorted(need - done)
        sources.append(
            {
                "source": source,
                "route": "B",
                "need": len(need),
                "need_scanned": len(scanned),
                "need_text": len(text),
                "done": len(need & done),
                "covered": not missing,
                "missing": missing,
                "missing_scanned": missing_scanned,
                "missing_text": missing_text,
            }
        )

    parsed_dir = workspace / "parsed"
    parse_calls = len(list(parsed_dir.glob("*/index.json"))) if parsed_dir.exists() else 0
    ocr_dir = workspace / "ocr"
    ocr_outputs = sum(len([p for p in d.glob("*.md") if p.name != "ocr_records.md"]) for d in ocr_dir.iterdir() if d.is_dir()) if ocr_dir.exists() else 0

    return {
        "sources": sources,
        "all_covered": all(s["covered"] for s in sources) if sources else False,
        "parse_calls": parse_calls,
        "ocr_outputs": ocr_outputs,
        "duplicate_parse_suspected": parse_calls > ocr_outputs,
    }


def summarize(report: dict) -> str:
    lines = []
    for s in report["sources"]:
        if s["route"] is None:
            lines.append(f"{s['source']}\troute=未选择\tcovered=False\t⛔ 需先由用户确认处理模式并回填 ocr_route")
            continue
        if s["route"] == "A":
            lines.append(f"{s['source']}\troute=A\tcovered={s['covered']}\tmissing={s['missing']}")
        else:
            lines.append(f"{s['source']}\troute=B\tneed={s['need']}(扫描{s['need_scanned']}+文本层{s['need_text']})\tdone={s['done']}\tcovered={s['covered']}\tmissing_scanned={s['missing_scanned']}\tmissing_text={s['missing_text']}")
    lines.append(f"parse_document 调用数(parsed/)={report['parse_calls']}\tOCR 产出={report['ocr_outputs']}\t重复解析嫌疑={report['duplicate_parse_suspected']}")
    if any(s["route"] is None for s in report["sources"]):
        lines.append("⛔ 存在未选择 `ocr_route` 的 source：**禁止**据此推断路线或按默认推进，必须先拿到用户的处理模式选择。")
    if any(s.get("missing_text") for s in report["sources"]):
        lines.append("⛔ 文本层页未归集：跑 `collect_text_pages.py --workspace <ws>` 机械归集，**禁止**为这些页派 OCR 子代理（未渲染图片，派了只会走 view_image 兜底白烧轮次）。文本层页「不需要 OCR」≠「不需要进证据库」。")
    if any(s.get("missing_scanned") for s in report["sources"]):
        lines.append("⚠️ 扫描页仍有缺口：只对 missing_scanned 补派 OCR（幂等：已有 .md 的页不得重跑）。")
    if not report["all_covered"]:
        lines.append("⚠️ 覆盖未完整，补全前不得越过本阶段。")
    if report["duplicate_parse_suspected"]:
        lines.append("⚠️ 解析调用数多于 OCR 产出：检查是否 A/B 双跑、整份+逐页重复、或解析了已提取完成的文档。")
    if report["all_covered"] and not report["duplicate_parse_suspected"]:
        lines.append("✅ 覆盖完整，无重复解析嫌疑。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OCR 覆盖率校验 + 重复解析自检")
    ap.add_argument("--workspace", required=True, help="workspace 目录")
    ap.add_argument("--json", help="可选：把完整报告写到该路径")
    args = ap.parse_args(argv)

    report = check(Path(args.workspace))
    print(summarize(report))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
