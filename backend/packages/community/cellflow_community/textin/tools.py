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
from .types import _IMAGE_EXTS, SUPPORTED_EXTS, ensure_supported, is_image

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


# --------------------------------------------------------------------------- #
# parse_image_batch                                                            #
# --------------------------------------------------------------------------- #

_MAX_CONCURRENCY = 3  # OCR provider concurrency ceiling (design: keep 2-3)

# Every page block in `ocr_records.md` starts with this line. It is the ONLY source of
# `evidence[].page` / `screenshot_ref` downstream (patient-separator's
# references/aggregate-ocr.md), and the aggregation script concatenates the per-page
# markdown verbatim — so whatever this tool writes IS the page block header.
#
# It used to be hand-written by the OCR subagent (pdf-image-extractor/SKILL.md). Once this
# tool took over the write, nobody wrote it: thread `1fee1395` produced 7 OCR pages with no
# provenance line at all, and that run's judgments contain ZERO `screenshot_ref`/`page`
# (thread `9a83ccc9`, same skill version, has 78/54). Deterministic code writes it now.
_PROVENANCE_PREFIX = "（来源图片："


def _provenance_line(src_path: str) -> str:
    """Virtual path only. Host absolute paths (`/Users/...`) break on any other deployment."""
    return f"{_PROVENANCE_PREFIX}{src_path}）"


def _has_provenance(text: str) -> bool:
    return text.lstrip().startswith(_PROVENANCE_PREFIX)


def _md_path(output_dir: str, image_name: str) -> str:
    """``x_page_001.jpg`` -> ``<output_dir>/x_page_001.md`` (same stem)."""
    stem = image_name.rsplit(".", 1)[0]
    return f"{output_dir.rstrip('/')}/{stem}.md"


def _page_markdown(doc, src_path: str) -> str:
    """Provenance line, then markdown, then tables — in that order.

    ``result.markdown`` does NOT contain tables (client.py contract #2) — appending
    them is what keeps a scanned lab-result page from silently losing every value.
    """
    parts = [f"{_provenance_line(src_path)}\n\n", doc.markdown or ""]
    for i, html in enumerate(doc.tables, start=1):
        parts.append(f"\n\n<!-- table {i:03d} -->\n{html}")
    return "".join(parts)


async def _existing_markdown(sandbox, md_path: str) -> str | None:
    """Existing NON-EMPTY markdown, else None.

    An empty file means the previous attempt failed; treating it as done would let
    the idempotency skip hide the failure forever.
    """
    try:
        existing = await asyncio.to_thread(sandbox.read_file, md_path)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return existing if (existing and existing.strip()) else None


async def _list_images(sandbox, input_dir: str) -> list[str]:
    try:
        hits, _ = await asyncio.to_thread(sandbox.glob, input_dir, "*")
    except Exception:
        logger.warning("parse_image_batch: glob failed for %s", input_dir, exc_info=True)
        hits = []
    names = [p.rsplit("/", 1)[-1] for p in hits]
    return sorted({n for n in names if is_image(n)})


@tool("parse_image_batch", parse_docstring=True)
async def parse_image_batch_tool(
    runtime: Runtime,
    input_dir: str,
    output_dir: str,
    overwrite: bool = False,
    concurrency: int = _MAX_CONCURRENCY,
) -> str:
    """OCR every image in a directory in ONE call, writing one markdown file per image.

    Use this instead of calling `parse_document` once per page image. For a 28-page
    scanned document the per-page loop costs 28 tool calls plus 28 read/write round
    trips — each one an extra model turn. This does the whole directory in one call.

    `<input_dir>/M018_page_001.jpg` becomes `<output_dir>/M018_page_001.md` (same stem).
    Non-image files (manifests, txt) are ignored.

    Each markdown starts with a provenance line `（来源图片：<虚拟路径>）`. Do NOT rewrite or
    strip it: downstream aggregation splits `ocr_records.md` into page blocks on that line,
    and it is the only source of `evidence[].page` / `screenshot_ref` when judging.

    Idempotent: images whose markdown already exists and is non-empty are skipped with
    NO provider call, so re-running after a partial failure only retries what failed.
    Legacy markdown that is missing the provenance line gets the line prepended in place —
    also with NO provider call, since the page was already parsed and billed.
    Pass `overwrite=True` to force a full re-OCR.

    Returns a COMPACT INDEX — counts, output dir, and the名单 of failed images — never
    the OCR text itself. Read what you need from the written markdown files afterwards
    (`grep` then `read_file`); dumping OCR text into the conversation would defeat the
    entire point of batching.

    A single failing image does not abort the batch: it is listed in the returned index
    with its provider error and writes no file, so it can be retried on its own.

    Args:
        input_dir: Absolute /mnt/user-data virtual directory holding the images.
        output_dir: Absolute /mnt/user-data virtual directory to write markdown into.
        overwrite: Re-OCR images that already have a non-empty markdown. Default False.
        concurrency: Parallel provider calls, clamped to 3 to protect the OCR service.
    """
    for label, path in (("input_dir", input_dir), ("output_dir", output_dir)):
        if not path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
            return f"Error: {label} must be under {VIRTUAL_PATH_PREFIX}/: {path!r}"

    cfg = _tool_settings()
    sandbox = await _ensure_sandbox(runtime)
    in_dir = input_dir.rstrip("/")
    out_dir = output_dir.rstrip("/")

    images = await _list_images(sandbox, in_dir)
    if not images:
        return f"No images found in {in_dir} (supported: {', '.join(sorted(SUPPORTED_EXTS & _IMAGE_EXTS))})"

    limit = max(1, min(int(concurrency or _MAX_CONCURRENCY), _MAX_CONCURRENCY))
    sem = asyncio.Semaphore(limit)
    skipped: list[str] = []
    repaired: list[str] = []
    written: list[str] = []
    failed: list[tuple[str, str]] = []

    async def handle(name: str) -> None:
        md_path = _md_path(out_dir, name)
        src = f"{in_dir}/{name}"
        if not overwrite:
            existing = await _existing_markdown(sandbox, md_path)
            if existing is not None:
                if _has_provenance(existing):
                    skipped.append(name)
                    return
                # Legacy output from before this tool wrote provenance. Prepend the line
                # instead of re-running OCR: a re-run bills the provider again for a page
                # already parsed, and leaving it alone keeps ocr_records.md page-block-less.
                try:
                    await asyncio.to_thread(sandbox.write_file, md_path, f"{_provenance_line(src)}\n\n{existing}")
                except Exception as exc:  # noqa: BLE001
                    failed.append((name, f"provenance repair failed: {exc}"))
                    return
                repaired.append(name)
                return
        async with sem:
            try:
                data = await asyncio.to_thread(sandbox.download_file, src, max_bytes=cfg["max_bytes"])
            except FileNotFoundError:
                failed.append((name, "file not found"))
                return
            except Exception as exc:
                failed.append((name, f"cannot read (limit {cfg['max_bytes']} bytes): {exc}"))
                return
            try:
                doc = await parse_via_textin(
                    data,
                    name,
                    app_id=cfg["app_id"],
                    secret_code=cfg["secret_code"],
                    base_url=cfg["base_url"],
                    timeout=cfg["timeout"],
                )
            except TextInError as exc:
                # Write nothing on failure: an empty .md would later be skipped as "done".
                failed.append((name, str(exc)))
                return
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill the batch
                failed.append((name, f"unexpected error: {exc}"))
                return
        try:
            await asyncio.to_thread(sandbox.write_file, md_path, _page_markdown(doc, src))
        except Exception as exc:  # noqa: BLE001
            failed.append((name, f"write failed: {exc}"))
            return
        written.append(name)

    await asyncio.gather(*(handle(n) for n in images))

    lines = [
        f"OCR batch done — {len(written)} written, {len(repaired)} repaired (provenance line added, no OCR re-run), {len(skipped)} skipped (already had markdown), {len(failed)} failed, {len(images)} images seen.",
        f"Output: {out_dir}/  (one .md per image, same stem; first line is `（来源图片：…）`)",
    ]
    if repaired:
        lines.append(f"Repaired (legacy output missing the provenance line): {', '.join(sorted(repaired)[:10])}{' …' if len(repaired) > 10 else ''}")
    if skipped and not overwrite:
        lines.append(f"Skipped (pass overwrite=True to redo): {', '.join(sorted(skipped)[:10])}{' …' if len(skipped) > 10 else ''}")
    if failed:
        lines.append("Failed (no file written; retry these individually):")
        lines.extend(f"  {name}: {reason}" for name, reason in sorted(failed))
    lines.append(f"Next: `grep -l <keyword> {out_dir}/*.md`, then read_file the match. OCR text is NOT returned here.")
    return "\n".join(lines)
