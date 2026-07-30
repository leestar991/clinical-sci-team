"""Per-thread source ledger (`sources.txt`) written by the recorded research tools.

Design (see DOCUSIGHT_CITATION_DESIGN.md, light / sandbox-script version):

- The ledger is a **sandbox-visible workspace file** so the in-sandbox
  ``resolve_citations.py`` script can read it. It is NOT process memory.
- Concurrency is only the in-process lead agent + <=3 in-process sub-researchers,
  so a single ``threading.Lock`` per sandbox serializes append + id allocation.
  **No CAS / Postgres / generation / fencing** — the blast radius of a race is one
  possibly-mis-numbered citation, self-healed by the script's dedup + dangling check.
- Recording is **best-effort**: if there is no sandbox / thread context, the caller
  records nothing and returns the un-recorded result (the search still works).

The core (``LedgerCore``) is decoupled from the real ``Sandbox`` via a tiny
read/write protocol so it is unit-testable without a sandbox.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Non-hidden filename on purpose: the AgentRun sandbox rejects dot-leading
# ("hidden") paths on write, and the agent itself handles this ledger — it must
# be plainly visible to `ls`, `read_file`, and resolve_citations.py.
# 扩展名用 .txt:AgentRun 沙箱 text 写不允许 .jsonl(内容仍是逐行 JSON)。
LEDGER_VPATH = "/mnt/user-data/workspace/sources.txt"

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "ref_src",
    "spm",
}


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def canonical_url(url: str) -> str:
    """Normalize a URL for dedup: lowercase scheme/host, drop ``www.``, strip
    tracking params + fragment, normalize trailing slash. Best-effort — returns
    the stripped input on parse failure."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.netloc:  # not an absolute http(s) URL (e.g. a PMID handle)
        return url.strip().lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Storage protocol (a real Sandbox satisfies this; tests pass a fake)
# --------------------------------------------------------------------------- #
class _LedgerIO(Protocol):
    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...


# --------------------------------------------------------------------------- #
# Core: dedup + monotonic id allocation, seeded from any existing file
# --------------------------------------------------------------------------- #
class LedgerCore:
    """In-memory dedup/id-allocation state for one ledger, backed by an ``_LedgerIO``.

    Not thread-safe on its own — callers hold the per-key lock (see ``append_source``).
    """

    def __init__(self, io: _LedgerIO, *, path: str = LEDGER_VPATH):
        self._io = io
        self._path = path
        self._hash_to_id: dict[str, int] = {}
        self._next_id = 1
        self._seeded = False

    def _seed(self) -> None:
        """Rebuild ``hash->id`` + ``next_id`` from an existing ledger file (cold start /
        process restart). Missing file → empty ledger."""
        try:
            content = self._io.read_file(self._path)
        except Exception:  # noqa: BLE001 - missing file / not-found is expected
            content = ""
        max_id = 0
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rid = int(rec["id"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            max_id = max(max_id, rid)
            url = rec.get("url")
            if url:
                canon = canonical_url(url)
                # keep the LOWEST id for a given URL so citations converge
                if canon not in self._hash_to_id or rid < self._hash_to_id[canon]:
                    self._hash_to_id[canon] = rid
        self._next_id = max_id + 1
        self._seeded = True

    def record(self, record: dict[str, Any]) -> int:
        """Dedup by canonical URL, allocate an id if new, append the line, return the id."""
        if not self._seeded:
            self._seed()
        url = record.get("url") or ""
        canon = canonical_url(url) if url else ""
        if canon and canon in self._hash_to_id:
            return self._hash_to_id[canon]
        rid = self._next_id
        self._next_id += 1
        full = {"id": rid, **record}
        self._io.write_file(self._path, json.dumps(full, ensure_ascii=False) + "\n", append=True)
        if canon:
            self._hash_to_id[canon] = rid
        return rid


# --------------------------------------------------------------------------- #
# Per-sandbox registry of cores + locks
# --------------------------------------------------------------------------- #
_cores: dict[str, LedgerCore] = {}
_locks: dict[str, threading.Lock] = {}
_registry_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _core_for(key: str, io: _LedgerIO) -> LedgerCore:
    core = _cores.get(key)
    if core is None:
        core = _cores[key] = LedgerCore(io)
    return core


def make_source_record(
    *,
    url: str,
    title: str | None = None,
    snippet: str | None = None,
    action: str,
    media: str = "web",
    tool_name: str,
    agent: str = "lead",
    query: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a SourceRecord (without ``id`` — assigned at append time)."""
    rec: dict[str, Any] = {
        "url": url,
        "url_raw": url,
        "domain": domain_of(url),
        "title": title,
        "snippet": snippet,
        "action": action,
        "media": media,
        "tool_name": tool_name,
        "agent": agent,
        "query": query,
        "created_at": _now_iso(),
    }
    if extra:
        rec.update(extra)
    return rec


def append_source(io: _LedgerIO, key: str, record: dict[str, Any]) -> int:
    """Thread-safe append for the ledger identified by ``key`` (per sandbox).

    Returns the assigned (or reused, on dedup) integer id.
    """
    lock = _lock_for(key)
    with lock:
        core = _core_for(key, io)
        return core.record(record)


def reset_for_tests() -> None:
    """Clear the in-process registry (unit tests only)."""
    with _registry_guard:
        _cores.clear()
        _locks.clear()


# --------------------------------------------------------------------------- #
# Runtime-facing entry points (used by the recorded tools)
# --------------------------------------------------------------------------- #
def _sandbox_and_key(runtime: object) -> tuple[_LedgerIO, str] | None:
    """Resolve the sandbox + a per-thread key from a tool runtime. Returns None if
    there is no sandbox/thread context (record is skipped, tool still returns)."""
    try:
        # Lazy import to avoid import cycles at module load.
        from deerflow.sandbox.tools import ensure_sandbox_initialized, ensure_thread_directories_exist

        # Order matters: ensure_thread_directories_exist reads the sandbox_id that
        # ensure_sandbox_initialized sets, and no-ops (never creating the workspace
        # dir) if the sandbox is not yet acquired. On the lazy path the recorded web
        # tool may be the first sandbox use, so init FIRST, then create the dirs —
        # otherwise the ledger write hits ENOENT on /mnt/user-data/workspace.
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
    except Exception as exc:  # noqa: BLE001 - no context / not initialized → skip recording
        logger.debug("source ledger: no sandbox context, skipping record (%s)", exc)
        return None
    key = getattr(sandbox, "id", None) or "unknown"
    return sandbox, str(key)


def _agent_label(runtime: object) -> str:
    """'lead' vs 'subagent' — best-effort from runtime context; defaults to 'lead'."""
    try:
        ctx = getattr(runtime, "context", None) or {}
        if ctx.get("is_subagent") or ctx.get("subagent_type"):
            return "subagent"
    except Exception:  # noqa: BLE001
        pass
    return "lead"


def record_from_runtime(runtime: object, *, agent: str | None = None, **record_kwargs: Any) -> int | None:
    """Record one source from a (sync) tool. Best-effort: returns the id, or None if
    there is no sandbox context or the append failed (never raises into the tool)."""
    resolved = _sandbox_and_key(runtime)
    if resolved is None:
        return None
    sandbox, key = resolved
    rec = make_source_record(agent=agent or _agent_label(runtime), **record_kwargs)
    try:
        return append_source(sandbox, key, rec)
    except Exception as exc:  # noqa: BLE001 - recording must never break the tool
        logger.warning("source ledger append failed (non-fatal): %s", exc)
        return None


def record_registry(
    runtime: object,
    registry: dict[str, dict] | None,
    *,
    tool_name: str,
    action: str = "open",
    media: str = "web",
) -> str:
    """Record every entry of a medical tool's ``source_registry``
    (``{src_id: {url, title, summary, ...}}``) to the ledger and return a Markdown
    'Sources' block that maps each to its ``[cite:N]`` id. Empty string if nothing
    was recorded (no context / empty registry). Extra fields (pmid/nct_id/nda) on an
    entry are carried onto the ledger record."""
    if not registry:
        return ""
    _META = {"url", "title", "summary"}
    lines: list[tuple[int, str, str]] = []
    for entry in registry.values():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not url:
            continue
        extra = {k: v for k, v in entry.items() if k not in _META and v is not None}
        rid = record_from_runtime(
            runtime,
            url=url,
            title=entry.get("title"),
            snippet=entry.get("summary"),
            action=action,
            media=media,
            tool_name=tool_name,
            extra=extra or None,
        )
        if rid is not None:
            lines.append((rid, entry.get("title") or url, url))
    if not lines:
        return ""
    body = "\n".join(f"[{rid}] {title} — {url}" for rid, title, url in lines)
    return f"\n\n---\nSources — cite each supported fact with its id, e.g. `[cite:{lines[0][0]}]`:\n{body}"


async def arecord_registry(runtime: object, registry: dict[str, dict] | None, **kwargs: Any) -> str:
    """Async variant of :func:`record_registry` — offloads sandbox IO off the event loop."""
    import asyncio

    return await asyncio.to_thread(record_registry, runtime, registry, **kwargs)


async def arecord_from_runtime(runtime: object, *, agent: str | None = None, **record_kwargs: Any) -> int | None:
    """Async variant — offloads the sync sandbox IO off the event loop (blocking-io gate)."""
    import asyncio

    return await asyncio.to_thread(record_from_runtime, runtime, agent=agent, **record_kwargs)
