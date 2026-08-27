#!/Volumes/data/github/clinical-sci-team/backend/.venv/bin/python
"""
将 PDF 文件导出为高分辨率图片。

用法:
    python pdf_to_image.py <pdf_path> [选项]

示例:
    # 基本用法（默认300DPI，PNG格式，输出到PDF同目录）
    python pdf_to_image.py /path/to/file.pdf

    # 指定DPI和输出目录
    python pdf_to_image.py /path/to/file.pdf --dpi 600 --output-dir /path/to/output

    # 输出JPEG格式，指定质量
    python pdf_to_image.py /path/to/file.pdf --format jpeg --quality 90

    # 批量处理目录下所有PDF
    python pdf_to_image.py /path/to/dir/ --recursive

    # 指定页码范围
    python pdf_to_image.py /path/to/file.pdf --pages 1-3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pypdfium2 as pdfium


def parse_pages(pages_str: str, total: int) -> list[int]:
    """解析页码范围字符串，返回0-based页码列表。

    支持格式: "1", "1-3", "1,3,5", "1-3,5,7-9"
    """
    if not pages_str:
        return list(range(total))

    result = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start_idx = max(0, int(start) - 1)
            end_idx = min(total, int(end))
            result.extend(range(start_idx, end_idx))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                result.append(idx)

    return sorted(set(result))


def extract_page_text(page) -> str:
    """提取单页的文本层内容（数字原生 PDF 有文本层，扫描件通常没有）。

    返回可提取的文本；扫描件/图片型页面返回空或极少字符。调用方据此判断
    该页是"文本型"（直接读文本，无需 OCR 图片编码）还是"扫描型"（需渲染
    图片送 Vision Model）。
    """
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range() or ""
    except Exception:
        return ""
    finally:
        try:
            textpage.close()
        except Exception:
            pass


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    dpi: int = 300,
    fmt: str = "png",
    quality: int = 95,
    pages: str | None = None,
    max_size_kb: int = 1024,
    text_mode: str = "auto",
    text_layer_threshold: int = 100,
) -> list[Path]:
    """将单个PDF文件转换为图片。

    Args:
        pdf_path: PDF文件路径
        output_dir: 输出目录
        dpi: 输出分辨率（默认300）
        fmt: 输出格式 png/jpeg（默认png）
        quality: JPEG质量 1-100（默认95）
        pages: 页码范围（默认全部）
        max_size_kb: 单张图片最大文件大小KB（默认1024=1MB），超过则自动降质/缩放
        text_mode: 文本层处理策略（token-budget-optimization P0 #2，命中中断 #2 主因）：
            - "auto"（默认）：逐页探测文本层，文本型页面（可提取字符 >= 阈值）直接
              写出 .txt 文本、跳过图片渲染与 base64 编码；仅扫描型页面渲染图片。
              这是最大的 token 收益点——OCR 图片 base64 占单患者筛选 ~69% 输入 token。
            - "image-only"：旧行为，所有页面一律渲染图片（不做文本层探测）。
            - "text-only"：所有页面一律抽文本层（不渲染图片），用于确认为纯文本 PDF。
        text_layer_threshold: auto 模式下判定"文本型页面"的最小可提取字符数（默认100）。
            低于该值视为扫描件/图片页，走图片渲染。

    Returns:
        生成的产物路径列表（文本型页面为 .txt，扫描型页面为图片文件）
    """
    pdf = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(pdf)
    page_indices = parse_pages(pages, total_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    scale = dpi / 72.0  # PDF默认72 DPI

    outputs: list[Path] = []
    manifest_pages: list[dict] = []

    for i in page_indices:
        page = pdf[i]

        # --- 文本层探测：文本型页面跳过图片编码（P0 #2 主收益） ---
        page_text = "" if text_mode == "image-only" else extract_page_text(page)
        text_chars = len(page_text.strip())
        is_text_page = text_mode == "text-only" or (text_mode == "auto" and text_chars >= text_layer_threshold)

        if is_text_page:
            if total_pages == 1:
                text_filename = f"{stem}.txt"
            else:
                text_filename = f"{stem}_page_{i + 1:03d}.txt"
            text_path = output_dir / text_filename
            text_path.write_text(page_text, encoding="utf-8")
            outputs.append(text_path)
            manifest_pages.append(
                {"page": i + 1, "filename": text_path.name, "format": "txt", "type": "text", "text_chars": text_chars}
            )
            print(f"  ✓ Page {i + 1}/{total_pages}: {text_filename} (text layer, {text_chars} chars — 跳过图片编码)")
            continue

        bitmap = page.render(scale=scale)
        img = bitmap.to_pil()

        # _save_within_limit 可能将 PNG 降级为 JPEG，返回实际使用的格式
        # 先探测实际格式以确定正确的文件扩展名
        actual_fmt = _save_within_limit(img, None, fmt, quality, max_size_kb)
        actual_ext = "jpg" if actual_fmt == "jpeg" else actual_fmt

        # 文件命名：单页PDF不加页码后缀
        if total_pages == 1:
            filename = f"{stem}.{actual_ext}"
        else:
            filename = f"{stem}_page_{i + 1:03d}.{actual_ext}"

        output_path = output_dir / filename

        # 实际保存到最终路径（由于输入不变，结果一致）
        _save_within_limit(img, output_path, fmt, quality, max_size_kb)
        outputs.append(output_path)
        manifest_pages.append(
            {"page": i + 1, "filename": output_path.name, "format": output_path.suffix.lstrip("."), "type": "scanned", "text_chars": text_chars}
        )

        size_kb = output_path.stat().st_size / 1024
        final_img_info = f"{img.width}×{img.height}"
        if size_kb > 1024:
            print(f"  ✓ Page {i + 1}/{total_pages}: {filename} ({final_img_info}, {size_kb / 1024:.1f} MB)")
        else:
            print(f"  ✓ Page {i + 1}/{total_pages}: {filename} ({final_img_info}, {size_kb:.0f} KB)")

    # Write manifest recording actual output filenames, formats, and page types
    text_page_count = sum(1 for p in manifest_pages if p["type"] == "text")
    scanned_page_count = sum(1 for p in manifest_pages if p["type"] == "scanned")
    manifest = {
        "source": pdf_path.name,
        "stem": stem,
        "total_pages": total_pages,
        "text_mode": text_mode,
        "text_pages": text_page_count,
        "scanned_pages": scanned_page_count,
        "pages": manifest_pages,
    }
    manifest_path = output_dir / f"{stem}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    pdf.close()
    return outputs


def _save_within_limit(
    img,
    output_path: Path | None,
    fmt: str,
    quality: int,
    max_size_kb: int,
) -> str:
    """保存图片，确保文件大小不超过 max_size_kb。

    返回实际使用的格式（"png" 或 "jpeg"）。
    当 output_path 为 None 时，仅探测最终格式但不写入文件。

    策略：
    1. 如果请求 PNG 格式，先尝试 PNG
    2. 如果 PNG 超限或请求 JPEG，使用 JPEG 并逐步降低质量（88→85→75→65→55→45）
    3. 如仍超限，逐步缩小尺寸（85%→70%→60%→50%→40%）+ 质量50
    """
    from io import BytesIO

    max_bytes = max_size_kb * 1024

    work_img = img
    if work_img.mode == "RGBA":
        work_img = work_img.convert("RGB")

    # 如果请求 PNG，先尝试 PNG
    if fmt.lower() == "png":
        buf = BytesIO()
        work_img.save(buf, "PNG", optimize=True)
        if buf.tell() <= max_bytes:
            if output_path is not None:
                with open(output_path, "wb") as f:
                    f.write(buf.getvalue())
            return "png"

    # PNG 超限或请求 JPEG，使用 JPEG
    # 第一次尝试：JPEG 指定质量
    save_quality = min(quality, 88)
    buf = BytesIO()
    work_img.save(buf, "JPEG", quality=save_quality, optimize=True)
    if buf.tell() <= max_bytes:
        if output_path is not None:
            with open(output_path, "wb") as f:
                f.write(buf.getvalue())
        return "jpeg"

    # 第二步：逐步降低 JPEG 质量
    for q in [85, 75, 65, 55, 45]:
        buf = BytesIO()
        work_img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= max_bytes:
            if output_path is not None:
                with open(output_path, "wb") as f:
                    f.write(buf.getvalue())
            return "jpeg"

    # 第三步：质量50 + 逐步缩小尺寸
    for scale_pct in [85, 70, 60, 50, 40]:
        new_w = int(work_img.width * scale_pct / 100)
        new_h = int(work_img.height * scale_pct / 100)
        resized = work_img.resize((new_w, new_h), resample=3)  # LANCZOS

        buf = BytesIO()
        resized.save(buf, "JPEG", quality=50, optimize=True)
        if buf.tell() <= max_bytes:
            if output_path is not None:
                with open(output_path, "wb") as f:
                    f.write(buf.getvalue())
            return "jpeg"

    # 最终兜底：40%尺寸 + 质量40
    final_w = int(work_img.width * 0.4)
    final_h = int(work_img.height * 0.4)
    resized = work_img.resize((final_w, final_h), resample=3)
    if output_path is not None:
        resized.save(output_path, "JPEG", quality=40, optimize=True)
    return "jpeg"


def collect_pdfs(path: Path, recursive: bool = False) -> list[Path]:
    """收集待处理的PDF文件列表。"""
    if path.is_file():
        if path.suffix.lower() == ".pdf":
            return [path]
        else:
            print(f"错误: {path} 不是PDF文件", file=sys.stderr)
            sys.exit(1)
    elif path.is_dir():
        pattern = "**/*.pdf" if recursive else "*.pdf"
        pdfs = sorted(path.glob(pattern))
        if not pdfs:
            print(f"错误: {path} 下未找到PDF文件", file=sys.stderr)
            sys.exit(1)
        return pdfs
    else:
        print(f"错误: {path} 不存在", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将PDF文件导出为高分辨率图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s poster.pdf                        # 默认300DPI PNG
  %(prog)s poster.pdf --dpi 600              # 600DPI高清
  %(prog)s poster.pdf --format jpeg -q 90    # JPEG格式
  %(prog)s /path/to/dir/ --recursive         # 批量处理
  %(prog)s paper.pdf --pages 1-3             # 仅前3页
        """,
    )
    parser.add_argument("path", type=Path, help="PDF文件路径或包含PDF的目录")
    parser.add_argument("--dpi", type=int, default=300, help="输出分辨率（默认: 300）")
    parser.add_argument("--format", "-f", choices=["png", "jpeg"], default="png", help="输出图片格式（默认: png）")
    parser.add_argument("--quality", "-q", type=int, default=95, help="JPEG质量 1-100（默认: 95）")
    parser.add_argument("--output-dir", "-o", type=Path, default=None, help="输出目录（默认: PDF同目录）")
    parser.add_argument("--pages", "-p", type=str, default=None, help="页码范围，如 1-3,5（默认: 全部）")
    parser.add_argument("--max-size", "-m", type=int, default=1024, help="单张图片最大KB（默认: 1024=1MB），超限自动压缩")
    parser.add_argument(
        "--text-mode",
        choices=["auto", "image-only", "text-only"],
        default="auto",
        help="文本层处理策略（默认: auto）。auto=文本型页面抽文本层跳过图片编码、扫描页渲染图片；image-only=旧行为全部渲染图片；text-only=全部抽文本层",
    )
    parser.add_argument(
        "--text-threshold",
        type=int,
        default=100,
        help="auto 模式下判定文本型页面的最小可提取字符数（默认: 100）",
    )
    parser.add_argument("--recursive", "-r", action="store_true", help="递归处理子目录中的PDF")

    args = parser.parse_args()

    pdfs = collect_pdfs(args.path, args.recursive)
    total_files = len(pdfs)

    print(f"📄 共 {total_files} 个PDF文件待处理")
    print(f"   DPI: {args.dpi} | 格式: {args.format.upper()} | 页码: {args.pages or '全部'} | 文本层: {args.text_mode}")
    print()

    t0 = time.time()
    total_images = 0

    for idx, pdf_path in enumerate(pdfs, 1):
        output_dir = args.output_dir if args.output_dir else pdf_path.parent
        print(f"[{idx}/{total_files}] {pdf_path.name}")

        try:
            outputs = convert_pdf(
                pdf_path=pdf_path,
                output_dir=output_dir,
                dpi=args.dpi,
                fmt=args.format,
                quality=args.quality,
                pages=args.pages,
                max_size_kb=args.max_size,
                text_mode=args.text_mode,
                text_layer_threshold=args.text_threshold,
            )
            total_images += len(outputs)
        except Exception as e:
            print(f"  ✗ 错误: {e}", file=sys.stderr)
            continue

        print()

    elapsed = time.time() - t0
    print(f"✅ 完成! 共生成 {total_images} 个产物（文本页 .txt + 扫描页图片），耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
