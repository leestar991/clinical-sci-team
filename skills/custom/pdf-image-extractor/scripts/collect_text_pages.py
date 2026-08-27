#!/usr/bin/env python3
"""把 PDF 文本层页的 `.txt` 机械归集成 `ocr/{source}/{stem}.md`。

为什么需要这一步
----------------
`pdf_to_image.py --text-mode auto` 逐页探测文本层：**文本型页面直接写 `.txt` 并跳过图片渲染**，
只有扫描型页面才渲染成图片。于是 `images/{source}/` 里会同时躺着 `.jpg`（待 OCR）和
`.txt`（已有文字，不需要 OCR）。OCR 子代理只处理图片，`.txt` 无人认领。

历史故障 thread `69612125`：`筛选期检查.pdf` 26 页里 11 页是文本层
（`page_016..026.txt`，共 32 KB 真实内容，含 `KRAS ... p.(G13D) 26.29%` 的基因检测报告）。
`ocr_coverage.py` 当时只把 15 个 scanned 页算进分母，报 `covered=True ✅ 覆盖完整`，
这 11 页从未进入 `ocr/`，也就从未进入 `ocr_records.md`。后果是 `IN-4-1` 被判
「无法判断：缺基因检测报告及RAS突变具体结果」——判定层准确指出了缺口，
而缺的正是被判为「已覆盖」的那 11 页。

设计约束
--------
- **逐字复制，绝不改写**：文本层是 PDF 内嵌的字符，本就是无损的；脚本只加来源标注。
  ⛔ 不生成 `key-fields` 速览 —— 那需要语义理解，脚本做等于编造
  （与「严禁 bash 脚本做语义修订」一致）。判定时读正文即可。
- **幂等**：目标 `.md` 已存在且非空则跳过，可安全重跑。
- **只认 manifest 里 `type == "text"` 的页**：不去猜目录里其它 `.txt`。
- 路线 A 的 source 整份解析已含文本层，跳过。

用法
----
    python3 collect_text_pages.py --workspace /mnt/user-data/workspace [--source 筛选期检查] [--json]

`exit 2` 表示有页归集失败（`.txt` 缺失或为空），其余情况 `exit 0`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _routes(workspace: Path) -> dict[str, str | None]:
    """{source_name: ocr_route}；与 `ocr_coverage.py` 同源口径，空值绝不当作 A。"""
    cls_path = workspace / "pdf_classification.json"
    routes: dict[str, str | None] = {}
    if cls_path.exists():
        try:
            cls = json.loads(cls_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cls = {}
        for item in (cls.get("scan") or []) + (cls.get("mixed") or []):
            if isinstance(item, dict) and item.get("source_name"):
                routes[item["source_name"]] = item.get("ocr_route") or None
    if routes:
        return routes
    images = workspace / "images"
    if images.exists():
        return {d.name: None for d in sorted(images.iterdir()) if d.is_dir()}
    return {}


def text_pages(workspace: Path, source: str) -> list[dict]:
    """manifest 中 `type == "text"` 的页记录（按页码升序）。"""
    mp = workspace / "images" / source / f"{source}_manifest.json"
    if not mp.exists():
        return []
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    pages = [p for p in (m.get("pages") or []) if isinstance(p, dict) and p.get("type") == "text"]
    return sorted(pages, key=lambda p: p.get("page") or 0)


#: 产物内引用路径统一用容器虚拟根（与扫描页 OCR 的来源标注、`ocr_page_index.py` 的
#: PROVENANCE_RE 契约一致）。沙箱执行命令前会把命令行里的 `/mnt/user-data/...` 重写成
#: 宿主机真实路径（否则脚本 open() 不到文件），但写进 `.md` 的来源行是**数据**，
#: local_sandbox 对数据文件不做反向翻译（`_DATA_CONTENT_EXTENSIONS`）——归集脚本必须
#: 自己把收到的（可能是宿主机形态的）workspace 换算回虚拟路径再落盘。会话 `156a476e`
#: 曾把宿主机绝对路径泄漏进 11 页产物，与扫描页的虚拟路径不一致。
VIRTUAL_USER_DATA_ROOT = "/mnt/user-data"


def virtual_user_data_path(path: Path, workspace: Path) -> str:
    """把 *path* 换算成 `/mnt/user-data/workspace/...` 形态的虚拟路径。

    *workspace* 是本脚本的 `--workspace`（容器虚拟形态或沙箱重写后的宿主机形态均可）：
    两者都指向 user-data 下的 workspace 目录，取 path 相对 workspace 的部分重新拼接即可。
    path 不在 workspace 下时原样返回，不硬造。
    """
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return str(path)
    return f"{VIRTUAL_USER_DATA_ROOT}/workspace/{rel.as_posix()}"


def render(txt_path: Path, page_no: int | None, text: str, workspace: Path) -> str:
    """统一内容格式：来源标注 + 逐字正文。

    来源标注与扫描页共用 `（来源图片：…）` 前缀 -- `ocr_records.md` 的页块靠这一个前缀切分，
    每种页型各写一套前缀会让下游要认多种写法（历史上共出现 4 种，见
    `tests/skills/test_ocr_provenance_contract.py`）。`文本层` 标识如实反映这页没经过 OCR。
    路径必须是 `virtual_user_data_path` 的虚拟形态，不能照抄 txt_path（见上方模块注释）。
    """
    head = f"（来源图片：{virtual_user_data_path(txt_path, workspace)} 文本层"
    if page_no:
        head += f"，第 {page_no} 页"
    head += "，PDF 内嵌文本逐字导出，未经 OCR；无 key-fields 速览，判定请读正文）"
    return f"{head}\n\n{text.rstrip()}\n"


def collect_source(workspace: Path, source: str) -> dict:
    img_dir = workspace / "images" / source
    ocr_dir = workspace / "ocr" / source
    written: list[str] = []
    skipped: list[str] = []
    problems: list[str] = []

    pages = text_pages(workspace, source)
    for rec in pages:
        filename = rec.get("filename")
        if not filename:
            continue
        stem = Path(filename).stem
        src = img_dir / filename
        dst = ocr_dir / f"{stem}.md"
        if dst.exists() and dst.read_text(encoding="utf-8").strip():
            skipped.append(stem)
            continue
        if not src.exists():
            problems.append(f"⛔ {source} 第 {rec.get('page')} 页文本层文件缺失：{src}")
            continue
        text = src.read_text(encoding="utf-8")
        if not text.strip():
            problems.append(f"⛔ {source} 第 {rec.get('page')} 页 `.txt` 为空：{src}（manifest 记 text_chars={rec.get('text_chars')}）→ 该页需改按扫描页渲染后 OCR")
            continue
        ocr_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(render(src, rec.get("page"), text, workspace), encoding="utf-8")
        written.append(stem)

    return {
        "source": source,
        "text_pages": len(pages),
        "written": written,
        "skipped": skipped,
        "problems": problems,
    }


def collect(workspace: Path, only: str | None = None) -> dict:
    results = []
    for source, route in sorted(_routes(workspace).items(), key=lambda kv: kv[0]):
        if only and source != only:
            continue
        if route == "A":
            results.append({"source": source, "route": "A", "note": "路线 A 整份解析已含文本层，跳过", "written": [], "skipped": [], "problems": [], "text_pages": 0})
            continue
        res = collect_source(workspace, source)
        res["route"] = route
        results.append(res)
    return {
        "sources": results,
        "total_written": sum(len(r["written"]) for r in results),
        "total_skipped": sum(len(r["skipped"]) for r in results),
        "problems": [p for r in results for p in r["problems"]],
    }


def summarize(report: dict) -> str:
    lines = []
    for r in report["sources"]:
        if r.get("note"):
            lines.append(f"{r['source']}\troute={r.get('route')}\t{r['note']}")
            continue
        lines.append(f"{r['source']}\t文本层页={r['text_pages']}\t新归集={len(r['written'])}\t幂等跳过={len(r['skipped'])}")
    lines.extend(report["problems"])
    if report["problems"]:
        lines.append("⛔ 有页归集失败，覆盖仍不完整；修好后重跑本脚本与 `ocr_coverage.py`。")
    elif report["total_written"]:
        lines.append(f"✅ 已归集 {report['total_written']} 页文本层内容到 `ocr/`；接着重跑 `ocr_coverage.py` 确认 covered=True 再汇总 `ocr_records.md`。")
    else:
        lines.append("✅ 无需归集（无文本层页，或全部已存在）。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 PDF 文本层页的 .txt 归集成 ocr/{source}/{stem}.md")
    ap.add_argument("--workspace", required=True, help="workspace 目录")
    ap.add_argument("--source", help="只处理指定 source")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    report = collect(Path(args.workspace), args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(summarize(report))
    return 2 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
