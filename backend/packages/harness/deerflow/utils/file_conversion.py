"""File conversion utilities.

Converts document files (PDF, PPT, Excel, Word) to Markdown using markitdown.
No FastAPI or HTTP dependencies — pure utility functions.
"""

import asyncio
import logging
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


def _convert_sync(file_path: Path) -> Path | None:
    """Blocking markitdown conversion — must be called from a thread pool."""
    from markitdown import MarkItDown

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
        # markitdown.convert() is CPU/IO-bound and blocking; run in a thread
        # pool to avoid stalling the FastAPI event loop on large documents.
        return await asyncio.to_thread(_convert_sync, file_path)
    except Exception as e:
        logger.error(f"Failed to convert {file_path.name} to markdown: {e}")
        return None
