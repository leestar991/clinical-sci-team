"""Recorded (same-named) variants of the web research tools.

Each tool delegates to the normal provider implementation, then records every
retrieved source to the per-thread ledger (`sources.txt`) and hands the model a
stable integer ``id`` per source so it can cite with ``[cite:N]``. See
DOCUSIGHT_CITATION_DESIGN.md and DOCUSIGHT_CITATION_TASKS.md (T2).

Provider assumption: these delegate to the providers docusight is wired to —
`tavily` (web_search), `tavily` (web_fetch), `image_search` (ddg). If the app-level
`config.yaml` points `web_search`/`web_fetch`/`image_search` at a different provider,
update the imports here (or generalize into a recording decorator — a documented
follow-on). Recording is best-effort: with no sandbox/thread context, the tool returns
the un-recorded result unchanged.
"""

from __future__ import annotations

import json
import logging
import re

from langchain.tools import tool

# Delegate to the docusight-wired provider implementations (unrecorded).
from cellflow_community.image_search.tools import image_search_tool as _orig_image_search
from cellflow_community.research import source_ledger as sl

# web_fetch uses Tavily Extract (AWS-hosted, reachable) instead of Jina Reader
# (r.jina.ai is Cloudflare-hosted and unreachable from the China deployment networks).
from cellflow_community.tavily.tools import web_fetch_tool as _orig_web_fetch
from cellflow_community.tavily.tools import web_search_tool as _orig_web_search
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

_MD_H1 = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _search_results(data: object) -> list | None:
    """Return the list of result dicts from a parsed web_search output — the bare-list
    shape (tavily/exa/ddg) or the wrapped ``{results: [...]}`` shape (serper). The list
    is a live reference into ``data`` so injecting ids mutates ``data`` in place."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return None


@tool("web_search", parse_docstring=True)
def web_search_recorded(query: str, runtime: Runtime = None) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    raw = _orig_web_search.func(query)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw  # error string / non-JSON → pass through unrecorded
    results = _search_results(data)
    if results is None:
        return raw  # unexpected shape → pass through unrecorded
    for r in results:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        rid = sl.record_from_runtime(
            runtime,
            url=r["url"],
            title=r.get("title"),
            snippet=r.get("snippet") or r.get("content") or r.get("description"),
            action="search",
            tool_name="web_search",
            query=query,
        )
        if rid is not None:
            r["id"] = rid
    return json.dumps(data, indent=2, ensure_ascii=False)  # preserve original shape


@tool("web_fetch", parse_docstring=True)
async def web_fetch_recorded(url: str, runtime: Runtime = None) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    # Tavily's web_fetch_tool is sync; .ainvoke runs it in a threadpool (won't block).
    content = await _orig_web_fetch.ainvoke({"url": url})
    if isinstance(content, str) and content.startswith("Error:"):
        return content  # fetch failed → nothing to record
    title = None
    if isinstance(content, str):
        m = _MD_H1.search(content)
        title = m.group(1) if m else None
    rid = await sl.arecord_from_runtime(
        runtime,
        url=url,
        title=title,
        snippet=(content[:280] if isinstance(content, str) else None),
        action="open",
        tool_name="web_fetch",
        query=None,
    )
    if rid is not None:
        return f"Source ID: {rid}\n\n{content}"
    return content


@tool("image_search", parse_docstring=True)
def image_search_recorded(
    query: str,
    max_results: int = 5,
    size: str | None = None,
    type_image: str | None = None,
    layout: str | None = None,
    runtime: Runtime = None,
) -> str:
    """Search for images online. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    **When to use:**
    - Before generating character/portrait images: search for similar poses, expressions, styles
    - Before generating specific objects/products: search for accurate visual references
    - Before generating scenes/locations: search for architectural or environmental references
    - Before generating fashion/clothing: search for style and detail references

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: Search keywords describing the images you want to find. Be specific for better results (e.g., "Japanese woman street photography 1990s" instead of just "woman").
        max_results: Maximum number of images to return. Default is 5.
        size: Image size filter. Options: "Small", "Medium", "Large", "Wallpaper". Use "Large" for reference images.
        type_image: Image type filter. Options: "photo", "clipart", "gif", "transparent", "line". Use "photo" for realistic references.
        layout: Layout filter. Options: "Square", "Tall", "Wide". Choose based on your generation needs.
    """
    raw = _orig_image_search.func(query, max_results=max_results, size=size, type_image=type_image, layout=layout)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return raw
    for r in results:
        img = r.get("image_url") or r.get("thumbnail_url") if isinstance(r, dict) else None
        if not img:
            continue
        rid = sl.record_from_runtime(
            runtime,
            url=img,
            title=r.get("title"),
            action="search",
            media="image",
            tool_name="image_search",
            query=query,
        )
        if rid is not None:
            r["id"] = rid  # per-image id — every returned image URL is recorded
    return json.dumps(data, indent=2, ensure_ascii=False)
