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


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    dpi: int = 300,
    fmt: str = "png",
    quality: int = 95,
    pages: str | None = None,
) -> list[Path]:
    """将单个PDF文件转换为图片。

    Args:
        pdf_path: PDF文件路径
        output_dir: 输出目录
        dpi: 输出分辨率（默认300）
        fmt: 输出格式 png/jpeg（默认png）
        quality: JPEG质量 1-100（默认95）
        pages: 页码范围（默认全部）

    Returns:
        生成的图片路径列表
    """
    pdf = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(pdf)
    page_indices = parse_pages(pages, total_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    ext = "jpg" if fmt.lower() == "jpeg" else fmt.lower()
    scale = dpi / 72.0  # PDF默认72 DPI

    outputs: list[Path] = []

    for i in page_indices:
        page = pdf[i]
        bitmap = page.render(scale=scale)
        img = bitmap.to_pil()

        # 文件命名：单页PDF不加页码后缀
        if total_pages == 1:
            filename = f"{stem}.{ext}"
        else:
            filename = f"{stem}_page_{i + 1:03d}.{ext}"

        output_path = output_dir / filename

        save_kwargs = {}
        if fmt.lower() == "jpeg":
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
            # JPEG不支持RGBA
            if img.mode == "RGBA":
                img = img.convert("RGB")

        img.save(output_path, fmt.upper(), **save_kwargs)
        outputs.append(output_path)

        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ Page {i + 1}/{total_pages}: {filename} ({img.width}×{img.height}, {size_mb:.1f} MB)")

    pdf.close()
    return outputs


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
    parser.add_argument("--recursive", "-r", action="store_true", help="递归处理子目录中的PDF")

    args = parser.parse_args()

    pdfs = collect_pdfs(args.path, args.recursive)
    total_files = len(pdfs)

    print(f"📄 共 {total_files} 个PDF文件待处理")
    print(f"   DPI: {args.dpi} | 格式: {args.format.upper()} | 页码: {args.pages or '全部'}")
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
            )
            total_images += len(outputs)
        except Exception as e:
            print(f"  ✗ 错误: {e}", file=sys.stderr)
            continue

        print()

    elapsed = time.time() - t0
    print(f"✅ 完成! 共生成 {total_images} 张图片，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
