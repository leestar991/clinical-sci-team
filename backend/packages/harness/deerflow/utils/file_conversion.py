"""File conversion utilities.

Converts document files (PDF, PPT, Excel, Word) to Markdown using markitdown.
No FastAPI or HTTP dependencies — pure utility functions.
"""

import asyncio
import logging
import re
from pathlib import Path

from deerflow.config.app_config import get_app_config

logger = logging.getLogger(__name__)

# File extensions that should be converted to markdown
CONVERTIBLE_EXTENSIONS = {
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".doc",
    ".docx",
}


_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MAX_OUTLINE_ITEMS = 50


def extract_outline(md_path: Path, max_items: int = _MAX_OUTLINE_ITEMS) -> list[dict]:
    """Extract markdown heading outline from a converted .md file.

    Args:
        md_path: Path to markdown file.
        max_items: Maximum number of headings to include.

    Returns:
        A list of dict entries: ``{"title": str, "line": int}``.
        When headings exceed ``max_items``, appends a sentinel
        ``{"truncated": True}`` at the end.
    """
    if not md_path.is_file():
        return []

    outline: list[dict] = []
    try:
        with md_path.open(encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                match = _HEADING_PATTERN.match(raw.strip())
                if not match:
                    continue
                title = match.group(2).strip()
                if not title:
                    continue
                outline.append({"title": title, "line": line_no})
                if len(outline) >= max_items:
                    outline.append({"truncated": True})
                    break
    except Exception:
        logger.debug("Failed to extract outline from %s", md_path, exc_info=True)
        return []

    return outline


<<<<<<< HEAD
def _convert_sync(file_path: Path) -> Path | None:
    """Blocking markitdown conversion — must be called from a thread pool."""
    from markitdown import MarkItDown
=======
def _get_uploads_config_value(key: str, default: object) -> object:
    """Read a value from the uploads config, supporting dict and attribute access."""
    cfg = get_app_config()
    uploads_cfg = getattr(cfg, "uploads", None)
    if isinstance(uploads_cfg, dict):
        return uploads_cfg.get(key, default)
    return getattr(uploads_cfg, key, default)


def _get_pdf_converter() -> str:
    """Read pdf_converter setting from app config, defaulting to 'auto'.
>>>>>>> 862cdec72d984a9c3e86fb908dbdbb3eacdb48a9

    md = MarkItDown()
    result = md.convert(str(file_path))
    md_path = file_path.with_suffix(".md")
    md_path.write_text(result.text_content, encoding="utf-8")
    logger.info(f"Converted {file_path.name} to markdown: {md_path.name}")
    return md_path


async def convert_file_to_markdown(file_path: Path) -> Path | None:
    """Convert a file to markdown using markitdown.

    Args:
        file_path: Path to the file to convert.

    Returns:
        Path to the markdown file if conversion was successful, None otherwise.
    """
    try:
<<<<<<< HEAD
        # markitdown.convert() is CPU/IO-bound and blocking; run in a thread
        # pool to avoid stalling the FastAPI event loop on large documents.
        return await asyncio.to_thread(_convert_sync, file_path)
    except Exception as e:
        logger.error(f"Failed to convert {file_path.name} to markdown: {e}")
        return None
=======
        raw = str(_get_uploads_config_value("pdf_converter", "auto")).strip().lower()
        if raw not in _ALLOWED_PDF_CONVERTERS:
            logger.warning("Invalid pdf_converter value %r; falling back to 'auto'", raw)
            return "auto"
        return raw
    except Exception:
        pass
    return "auto"
>>>>>>> 862cdec72d984a9c3e86fb908dbdbb3eacdb48a9
