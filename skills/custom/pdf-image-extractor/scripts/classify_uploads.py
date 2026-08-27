#!/usr/bin/env python3
"""上传文件归类（输入预检 + PDF 类型判定 + 路线预设）。

把"哪些文件该 OCR、走哪条路线、哪些必须忽略"这件事收敛成一个确定性脚本，
避免编排层每次用内联 python 重写一遍（规则漂移的根源）。

三件事一次做完：

1. **输入预检（剔除）**
   - `size == 0` → 空文件 / 上传中间件转换失败的产物。既不解析也不 read_file。
     （历史故障：流程反复读一个 0 字节 `.md` 直至卡死，而同名 PDF 早已拆图完成。）
   - **sidecar `.md`** → 与某个 `.pdf/.docx/.doc/.xlsx/.pptx` 同 stem 的 `.md`，
     是上传中间件的转换产物，不是独立输入；只作为原文件"有无文本层"的判据。

2. **PDF 类型判定**（依据 sidecar `.md` 大小）
   | 条件 | 类型 | 处理 |
   |---|---|---|
   | `md_size == 0` | `scan` | 全扫描页 → 需要 pdf_to_image 拆页 |
   | `md_size > 0` 且 `pdf_size / md_size > 20` | `mixed` | 部分文字 + 大量扫描页 → 也需拆页 |
   | 其余 | `text` | 文字为主 → 直接读 sidecar `.md`，不拆页、不 OCR |

3. **解析路线占位（不预设默认值）**：`scan` / `mixed` 每个 source 写 `ocr_route = null` +
   `route_reason = null`，表示**尚未决定**。路线必须由**用户显式选择处理模式**后回填
   （`A` = 整份一次解析 / `B` = 逐页图像解析）。
   ⛔ 脚本**故意不给默认值**：默认值会被下游当成"已确认"，历史上出现过模型凭空写
   `route_reason: "用户已确认单患者模式"` 直接推进的情况。`null` 让"未选择"在数据上不可伪装。

**幂等**：`--out` 已存在时，保留其中已有的 `ocr_route` / `route_reason` /
`role` / `handled_by` 值，不覆盖人工（LLM）判定结果。

用法：
    python3 classify_uploads.py --uploads /mnt/user-data/uploads \
        --out /mnt/user-data/workspace/pdf_classification.json \
        [--images-dir /mnt/user-data/workspace/images]

`--images-dir` 可选：若已拆页，从各 `{source}_manifest.json` 回填
`total_pages` / `scanned_pages`（`scanned_pages` 才是 OCR 工作量与覆盖率的分母）。

始终 exit 0；结果以产物 JSON 与 stdout 摘要为准。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DOC_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}
MIXED_RATIO = 20  # pdf_size / md_size 超过该比值 → 判为 mixed
# 不预设默认路线：ocr_route 必须由用户选择处理模式后回填（见模块 docstring 第 3 点）
PENDING_ROUTE = None

# 文件名关键词 → role 提示（仅提示，最终 role 由调用方确认）
PROTOCOL_HINTS = ("入排", "排除标准", "入选标准", "方案", "protocol", "criteria", "eligibility")


def _role_hint(name: str) -> str:
    lowered = name.lower()
    return "protocol_criteria" if any(h.lower() in lowered for h in PROTOCOL_HINTS) else "patient_record"


def _pdf_type(pdf_size: int, md_size: int) -> str:
    if md_size == 0:
        return "scan"
    if pdf_size / md_size > MIXED_RATIO:
        return "mixed"
    return "text"


def _manifest_pages(images_dir: Path | None, source: str) -> tuple[int | None, int | None]:
    """从 {source}_manifest.json 取 (total_pages, scanned_pages)；取不到返回 (None, None)。"""
    if images_dir is None:
        return None, None
    mp = images_dir / source / f"{source}_manifest.json"
    if not mp.exists():
        return None, None
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    pages = m.get("pages") or []
    scanned = m.get("scanned_pages")
    if scanned is None:
        scanned = sum(1 for p in pages if isinstance(p, dict) and p.get("type") == "scanned")
    return m.get("total_pages"), scanned


def _previous(out_path: Path) -> dict[str, dict]:
    """读旧产物，返回 {文件名: 条目} 以便保留人工判定字段。"""
    if not out_path.exists():
        return {}
    try:
        old = json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    prev: dict[str, dict] = {}
    for key in ("scan", "mixed", "text"):
        for item in old.get(key) or []:
            if isinstance(item, dict) and item.get("pdf"):
                prev[item["pdf"]] = item
    for item in old.get("non_pdf") or []:
        if isinstance(item, dict) and item.get("file"):
            prev[item["file"]] = item
    return prev


def classify(uploads: Path, images_dir: Path | None = None, previous: dict | None = None) -> dict:
    previous = previous or {}
    files = sorted(p for p in uploads.iterdir() if p.is_file()) if uploads.exists() else []
    doc_stems = {p.stem for p in files if p.suffix.lower() in DOC_EXT}

    result: dict[str, list] = {"scan": [], "mixed": [], "text": [], "non_pdf": [], "ignored": []}

    for p in files:
        size = p.stat().st_size
        suffix = p.suffix.lower()

        # 1) 零字节（含中间件转换失败的 sidecar）→ 剔除，禁止后续 read_file / parse_document
        if size == 0:
            result["ignored"].append({"file": p.name, "reason": "size=0（空文件 / 上传中间件转换失败）"})
            continue
        # 2) 非零字节 sidecar → 不是独立输入，只作原文件 md_size 判据
        if suffix == ".md" and p.stem in doc_stems:
            result["ignored"].append(
                {"file": p.name, "reason": f"{p.stem} 的 sidecar 转换产物，已作为该文件的 md_size 判据"}
            )
            continue

        old = previous.get(p.name, {})
        if suffix == ".pdf":
            md = p.with_suffix(".md")
            md_size = md.stat().st_size if md.exists() else 0
            entry = {
                "pdf": p.name,
                "source_name": p.stem,
                "pdf_size": size,
                "md_size": md_size,
            }
            ptype = _pdf_type(size, md_size)
            if ptype in ("scan", "mixed"):
                total, scanned = _manifest_pages(images_dir, p.stem)
                if total is not None:
                    entry["total_pages"] = total
                if scanned is not None:
                    entry["scanned_pages"] = scanned
                # 不预设默认路线：保留已有值，否则留 null 表示"待用户选择模式后回填"
                entry["ocr_route"] = old.get("ocr_route") or PENDING_ROUTE
                entry["route_reason"] = old.get("route_reason") or PENDING_ROUTE
            else:
                entry["handled_by"] = "read_md"
            result[ptype].append(entry)
        else:
            sidecar = p.with_suffix(".md")
            result["non_pdf"].append(
                {
                    "file": p.name,
                    "sidecar_md": sidecar.name if sidecar.exists() else None,
                    "sidecar_md_size": sidecar.stat().st_size if sidecar.exists() else 0,
                    "role": old.get("role") or _role_hint(p.name),
                    "handled_by": old.get("handled_by"),
                }
            )

    return result


def summarize(result: dict) -> str:
    lines = []
    for key in ("scan", "mixed"):
        for item in result[key]:
            route = item.get("ocr_route") or "未选择(待用户确认处理模式)"
            lines.append(f"{key}\t{item['pdf']}\troute={route}\tscanned_pages={item.get('scanned_pages', '?')}")
    for item in result["text"]:
        lines.append(f"text\t{item['pdf']}\t→ 直接读 sidecar {Path(item['pdf']).stem}.md（不 OCR）")
    for item in result["non_pdf"]:
        lines.append(f"non_pdf\t{item['file']}\trole={item['role']}\thandled_by={item['handled_by']}")
    for item in result["ignored"]:
        lines.append(f"ignored\t{item['file']}\t{item['reason']}")
    return "\n".join(lines) if lines else "(uploads 为空)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="上传文件归类（预检 + PDF 类型 + 路线预设）")
    ap.add_argument("--uploads", required=True, help="uploads 目录")
    ap.add_argument("--out", required=True, help="输出 pdf_classification.json 路径")
    ap.add_argument("--images-dir", help="可选：images 目录，用于从 manifest 回填页数")
    args = ap.parse_args(argv)

    out_path = Path(args.out)
    result = classify(
        Path(args.uploads),
        Path(args.images_dir) if args.images_dir else None,
        _previous(out_path),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(summarize(result))
    print(f"\n产物已写入：{out_path}")
    if result["non_pdf"]:
        print("⚠️ `non_pdf` 的 role/handled_by 为提示值，需确认后回写（protocol_criteria 类文档一旦提取完成即禁止再解析）。")
    if result["ignored"]:
        print("⚠️ `ignored` 段文件在后续所有阶段禁止 read_file / parse_document。")
    pending = [i["pdf"] for i in result["scan"] + result["mixed"] if not i.get("ocr_route")]
    if pending:
        print(f"⛔ 以下 source 的 `ocr_route` 尚未决定：{pending}。**必须先让用户显式选择处理模式**，再回填 A/B；脚本不提供默认值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
