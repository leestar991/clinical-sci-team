"""Artifact layout + content-addressed cache.

SPEC §9.1 INVARIANT: every read/write here goes through the ``Sandbox`` API.
Object-storage deployments have no local ``/mnt/user-data`` on the gateway, and
the four providers resolve virtual paths differently — so ``open()`` /
``Path.write_text`` / ``os.path.exists`` are FORBIDDEN on these paths. Sandbox
methods are sync, hence every call is wrapped in ``asyncio.to_thread``.

Layout (thread-scoped, lives with the workspace):
    /mnt/user-data/workspace/parsed/<sha256(bytes)[:12]>/
        document.md
        tables/001.html …
        index.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os  # noqa: F401  — imported so the invariant test can assert os.path is unused
import re

from deerflow.config.paths import VIRTUAL_PATH_PREFIX

from .types import ParsedDoc

logger = logging.getLogger(__name__)

PARSED_ROOT = f"{VIRTUAL_PATH_PREFIX}/workspace/parsed"
_PREVIEW_CHARS = 80


def cache_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def artifact_dir(key: str) -> str:
    return f"{PARSED_ROOT}/{key}"


def _preview(html: str) -> str:
    """First bit of visible text in an HTML table, for the index."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())[:_PREVIEW_CHARS]


def _render_index(key: str, meta: dict) -> str:
    lines = [
        f"Parsed with `{meta['parser']}` — {meta['pages']} page(s), {meta['table_count']} table(s).",
        f"Artifacts: {artifact_dir(key)}/",
        "  document.md          full markdown (NOT returned here — read it if you need prose)",
        "  tables/NNN.html      one HTML table per file",
        "  index.json           this index",
    ]
    if meta["tables"]:
        lines.append("Tables:")
        for t in meta["tables"]:
            lines.append(f"  [{t['i']:03d}] {t['preview']}")
    lines.append(f"Next: `grep -l <keyword> {artifact_dir(key)}/tables/*.html`, then read_file the match.")
    return "\n".join(lines)


async def read_cached_index(sandbox, key: str) -> str | None:
    """Return the rendered index if this content was already parsed, else None."""
    path = f"{artifact_dir(key)}/index.json"
    try:
        raw = await asyncio.to_thread(sandbox.read_file, path)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("parse_document: cache probe failed for %s", path, exc_info=True)
        return None
    try:
        return _render_index(key, json.loads(raw))
    except Exception:
        return None


async def write_artifacts(sandbox, key: str, doc: ParsedDoc) -> str:
    """Write all artifacts through the Sandbox API and return the rendered index."""
    base = artifact_dir(key)
    await asyncio.to_thread(sandbox.write_file, f"{base}/document.md", doc.markdown)

    tables_meta = []
    for i, html in enumerate(doc.tables, start=1):
        await asyncio.to_thread(sandbox.write_file, f"{base}/tables/{i:03d}.html", html)
        tables_meta.append({"i": i, "preview": _preview(html)})

    meta = {"parser": doc.parser, "pages": doc.pages, "table_count": len(doc.tables), "tables": tables_meta}
    await asyncio.to_thread(sandbox.write_file, f"{base}/index.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return _render_index(key, meta)
