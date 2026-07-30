"""``parse_document`` — gateway-side document parsing via 合合 (TextIn).

Why this exists: without it an agent must ``pip install`` a PDF library inside
the sandbox on every run (observed: pymupdf installed twice, then abandoned for
pypdf, then 96 financial numbers hand-typed because nothing could read the
tables). Parsing here needs no sandbox image change.

No local fallback this round (design §4): a TextIn failure returns an explicit
error rather than a degraded, table-less result that could be mistaken for a
good one.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import tool

from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.tools.types import Runtime

from .artifacts import cache_key, read_cached_index, write_artifacts
from .client import TextInError, parse_via_textin
from .types import ensure_supported

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.textin.com"
_DEFAULT_TIMEOUT = 300.0
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024


def _tool_settings() -> dict:
    """Read this tool's own config entry (same pattern as the tavily tools)."""
    cfg = get_app_config().get_tool_config("parse_document")
    extra = cfg.model_extra if cfg is not None else {}
    return {
        "app_id": extra.get("app_id") or "",
        "secret_code": extra.get("secret_code") or "",
        "base_url": extra.get("base_url") or _DEFAULT_BASE_URL,
        "timeout": float(extra.get("timeout") or _DEFAULT_TIMEOUT),
        "max_bytes": int(extra.get("max_bytes") or _DEFAULT_MAX_BYTES),
    }


async def _ensure_sandbox(runtime: Runtime):
    # Lazy import — module-level would create a circular import
    # (sandbox.tools → tools.builtins → view_image_tool → sandbox.tools).
    from deerflow.sandbox.tools import ensure_sandbox_initialized_async

    return await ensure_sandbox_initialized_async(runtime)


@tool("parse_document", parse_docstring=True)
async def parse_document_tool(runtime: Runtime, path: str) -> str:
    """Parse a document into searchable artifacts and return an index of what was found.

    Use this for ANY document you need facts out of — PDF, Word, PowerPoint, Excel,
    or a scanned image. It replaces installing PDF libraries in the sandbox.

    When NOT to use it: for plain text / markdown / code files (use `read_file`),
    and never as a way to dump a document into the conversation.

    Returns an INDEX — page count, table count, artifact paths, and a one-line
    preview per table — NOT the document text. A 232-page report produces ~400k
    characters of markdown, which must never enter the conversation. Read what you
    need from the artifacts: `grep -l <keyword> <dir>/tables/*.html`, then
    `read_file` the match; full prose is in `<dir>/document.md`.

    On failure it returns an explicit `Error:` line and parses nothing — treat that
    as the document being unavailable; do NOT fall back to installing PDF libraries.

    Args:
        path: Absolute /mnt/user-data virtual path of the document to parse.
    """
    if not path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        return f"Error: path must be under {VIRTUAL_PATH_PREFIX}/: {path!r}"

    filename = path.rsplit("/", 1)[-1]
    try:
        ensure_supported(filename)
    except ValueError as exc:
        return f"Error: {exc}"

    cfg = _tool_settings()
    sandbox = await _ensure_sandbox(runtime)

    try:
        data = await asyncio.to_thread(sandbox.download_file, path, max_bytes=cfg["max_bytes"])
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as exc:
        return f"Error: cannot read {path} (limit {cfg['max_bytes']} bytes): {exc}"

    key = cache_key(data)
    cached = await read_cached_index(sandbox, key)
    if cached is not None:
        logger.info("parse_document: cache hit for %s (%s)", path, key)
        return cached

    try:
        doc = await parse_via_textin(
            data,
            filename,
            app_id=cfg["app_id"],
            secret_code=cfg["secret_code"],
            base_url=cfg["base_url"],
            timeout=cfg["timeout"],
        )
    except TextInError as exc:
        logger.warning("parse_document: TextIn failed for %s: %s", path, exc)
        return f"Error: document parsing failed for {filename}: {exc}"

    return await write_artifacts(sandbox, key, doc)
