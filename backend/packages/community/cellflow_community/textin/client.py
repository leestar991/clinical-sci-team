"""合并 (TextIn) API client.

Contract verified live on 2026-07-28:
  POST {base_url}/ai/service/v1/pdf_to_markdown   (**all** inputs — pdf, office
      formats AND images; TextIn sniffs file_type itself, so no per-extension
      endpoint table is needed)
  headers: x-ti-app-id / x-ti-secret-code ; body: raw file bytes
  response: {code, msg, result:{markdown, total_page_number, pages:[{structured:[...]}]}}

THREE contract details that are easy to get wrong:
  1. Failure is reported as **HTTP 200 with body code != 200** (e.g. 40425).
  2. Tables are NOT in ``result.markdown`` (a 232-page report yielded ZERO pipe
     tables) — they live in ``result.pages[].structured[type=="table"].text``
     as HTML (373 of them). Reading only ``markdown`` silently drops every table.
  3. There is **no ``image_to_markdown`` robot**. Posting an image to
     ``/ai/service/v1/image_to_markdown`` returns ``code=40007 机器人不存在或未发布``
     (verified 2026-07-28 with a synthetic 200x200 PNG), which made every
     per-page-image OCR call fail while whole-PDF parsing worked. ``pdf_to_markdown``
     accepts images directly (same PNG → ``code=200, total_page_number=1``); its
     error table even carries image-specific codes (40301 图片类型不支持 /
     40304 图片尺寸不符). Route **everything** through ``pdf_to_markdown``.
"""

from __future__ import annotations

import logging

import httpx

from .types import ParsedDoc

logger = logging.getLogger(__name__)


class TextInError(Exception):
    """TextIn call failed (credentials, transport, or body code != 200)."""


# Single universal endpoint. Do NOT reintroduce per-extension routing: the
# ``image_to_markdown`` robot does not exist and returns code=40007 (see module
# docstring note 3).
_MARKDOWN_LEAF = "pdf_to_markdown"


def _endpoint(base_url: str, filename: str) -> str:
    # ``filename`` is kept in the signature for logging/compat; TextIn sniffs the
    # real file type from the request body itself.
    return f"{base_url.rstrip('/')}/ai/service/v1/{_MARKDOWN_LEAF}"


async def parse_via_textin(
    data: bytes,
    filename: str,
    *,
    app_id: str,
    secret_code: str,
    base_url: str,
    timeout: float,
) -> ParsedDoc:
    """Parse *data* via TextIn. Raises TextInError on any failure."""
    if not app_id or not secret_code:
        raise TextInError("TextIn credentials are not configured (TEXTIN_APP_ID / TEXTIN_SECRET_CODE)")

    url = _endpoint(base_url, filename)
    headers = {"x-ti-app-id": app_id, "x-ti-secret-code": secret_code}
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.post(url, content=data, headers=headers)
            payload = resp.json()
    except Exception as exc:
        raise TextInError(f"TextIn request failed: {exc}") from exc

    code = payload.get("code")
    if code != 200:
        raise TextInError(f"TextIn returned code={code}: {payload.get('msg')}")

    result = payload.get("result") or {}
    tables: list[str] = []
    for page in result.get("pages") or []:
        for block in page.get("structured") or []:
            if block.get("type") == "table" and block.get("text"):
                tables.append(block["text"])

    return ParsedDoc(
        markdown=result.get("markdown", "") or "",
        tables=tables,
        pages=int(result.get("total_page_number") or 0),
        parser="textin",
    )
