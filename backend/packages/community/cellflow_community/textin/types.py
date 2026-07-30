"""Shared types + supported-format gate for the TextIn document parser."""

from __future__ import annotations

from dataclasses import dataclass, field

# Verified live 2026-07-21: TextIn sniffs file_type itself and returned code=200
# for pdf / docx / pptx / xlsx / xls / doc / ppt. Images use a different endpoint.
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"})
_DOC_EXTS = frozenset({".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"})
SUPPORTED_EXTS = _IMAGE_EXTS | _DOC_EXTS


@dataclass
class ParsedDoc:
    """Normalised parse result."""

    markdown: str
    tables: list[str] = field(default_factory=list)  # each entry is an HTML <table>
    pages: int = 0
    parser: str = ""  # "textin" (kept so a future parser can be distinguished)


def _ext(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


def is_image(filename: str) -> bool:
    return _ext(filename) in _IMAGE_EXTS


def ensure_supported(filename: str) -> None:
    """Raise ValueError if *filename* is not a document TextIn can parse."""
    ext = _ext(filename)
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"unsupported document type {ext or filename!r}; supported: {', '.join(sorted(SUPPORTED_EXTS))}")
