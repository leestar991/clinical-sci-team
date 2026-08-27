"""Version-aware ``read_file`` deduplication (criteria-token-saving Task 5).

Why: in the measured baseline, 147 externalised ``read_file`` payloads carried only 62
distinct content hashes — 2,479,270 bytes (63.6%) were byte-identical re-reads. The same
``SKILL.md`` or judgment JSON gets read whole, repeatedly, inside a single run.

What it does: on a repeat ``read_file`` of the same ``(sandbox, thread, run, path, range)``
whose content hash is unchanged, return a short reference instead of the body.

Correctness first. The cache key contains the content hash, so any modification is a
natural miss — there is no invalidation to forget. Writes additionally drop the file's
entries, which matters for the ``read -> str_replace -> read`` loop where the second read
must show the edit. A stale hit would be worse than the tokens it saves: the model would
go on to edit content that no longer exists.

**Disabled by default** (``read_file_dedup.enabled: false``). Swapping file content for a
reference changes what the model sees, so it is opted into per deployment after a replay
comparison rather than switched on by an upgrade.

Scope: this middleware never suppresses the FIRST read, small reads
(``min_chars``), non-read tools, or error results.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.read_dedup_config import ReadFileDedupConfig

logger = logging.getLogger(__name__)

_READ_TOOLS = frozenset({"read_file"})
_WRITE_TOOLS = frozenset({"write_file", "str_replace", "apply_json_patches"})

# ToolOutputBudgetMiddleware externalises oversized payloads and appends
# ``[Full read_file output saved to <virtual path> (...)]`` to the message. It sits
# OUTSIDE this middleware, so the first read's marker only exists in the transcript, never
# in what this middleware sees. Resolving it by tool_call_id is what lets a dedup reference
# say *where the bytes are* instead of just "omitted".
_EXTERNALIZED_PATH = re.compile(r"output saved to (\S+?)(?:\s|\)|,|$)")

# Module-level so it survives across middleware instances within a process, the same way
# the agent (and therefore its middleware chain) is cached and reused across runs.
# Keyed tightly enough that no two contexts can collide — see _cache_key.
_cache: OrderedDict[tuple, str] = OrderedDict()
_cache_lock = threading.Lock()


def _reset_dedup_cache() -> None:
    """Test helper: drop all entries."""
    with _cache_lock:
        _cache.clear()


def _dedup_cache_size() -> int:
    with _cache_lock:
        return len(_cache)


def _content_hash(content: str) -> str:
    """Same strategy as read_before_write and textin.artifacts, so "which version of this
    file" means one thing across the codebase."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _looks_like_error(content: str) -> bool:
    return content.lstrip().startswith("Error:")


class ReadFileDedupMiddleware(AgentMiddleware):
    """Suppress redundant identical ``read_file`` payloads within a run."""

    def __init__(self, config: ReadFileDedupConfig | None = None) -> None:
        super().__init__()
        self._config = config or ReadFileDedupConfig()

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _context(request: ToolCallRequest) -> dict:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _requested_path(request: ToolCallRequest) -> str | None:
        args = request.tool_call.get("args") or {}
        if not isinstance(args, dict):
            return None
        path = args.get("path")
        return path if isinstance(path, str) and path else None

    def _cache_key(self, request: ToolCallRequest, path: str, content_hash: str) -> tuple:
        args = request.tool_call.get("args") or {}
        return (
            *self._scope(request),
            posixpath.normpath(path),
            args.get("start_line"),
            args.get("end_line"),
            content_hash,
        )

    def _scope(self, request: ToolCallRequest) -> tuple:
        """The isolation boundary a cached read belongs to.

        ``task_id`` is what keeps subagents honest. Their contexts are isolated — task B's
        transcript does not contain task A's read — so a cache keyed only by
        (sandbox, thread, run) would hand B a "you already read this, content omitted"
        reference for a file it has never seen and cannot retrieve. That starves exactly
        the judgment/QC subagents this dedup was meant to speed up. The lead agent has no
        ``task_id`` (``None``), so it keeps its own scope and its behaviour is unchanged.

        ``run_id`` stays in the key for a different reason: a file can be changed between
        runs by something other than this agent, and a "you already read this" reference
        has no referent in a fresh run's transcript.
        """
        context = self._context(request)
        return (
            context.get("sandbox_id"),
            context.get("thread_id"),
            context.get("run_id"),
            context.get("task_id"),
        )

    def _invalidate_path(self, request: ToolCallRequest, path: str) -> None:
        prefix = (*self._scope(request), posixpath.normpath(path))
        width = len(prefix)
        with _cache_lock:
            for key in [k for k in _cache if k[:width] == prefix]:
                del _cache[key]

    @staticmethod
    def _first_read_message(request: ToolCallRequest, first_call_id: str | None) -> tuple[ToolMessage | None, bool]:
        """Locate the ``ToolMessage`` the FIRST read produced.

        Returns ``(message, searchable)``. ``searchable`` is ``False`` when the transcript
        could not be inspected at all (no ``state``, no ``messages``, no recorded call id)
        — then ``message is None`` means "unknown", not "gone", and callers must not read
        an absence into it. When ``searchable`` is ``True``, ``message is None`` is
        evidence: the first read is no longer in the context the model will see.

        Looked up by ``tool_call_id`` rather than by filename: the externalised artifact
        name is derived from the call id, and matching on the read path would pick the
        wrong artifact when the same file was read at several ranges.
        """
        if not first_call_id:
            return None, False
        state = getattr(request, "state", None)
        messages = state.get("messages") if isinstance(state, dict) else None
        if not messages:
            return None, False
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.tool_call_id == first_call_id:
                return message, True
        return None, True

    @staticmethod
    def _externalized_path(first_read: ToolMessage | None) -> str | None:
        """Where the first read's full payload landed, if it was externalised to disk."""
        if first_read is None:
            return None
        content = first_read.content if isinstance(first_read.content, str) else ""
        found = _EXTERNALIZED_PATH.search(content)
        return found.group(1) if found else None

    def _reference(self, path: str, externalized: str | None = None) -> str:
        """What the model reads instead of a byte-identical payload.

        ⚠️ Every sentence here is an instruction the model will follow literally, so each one
        has to be judged by what it makes the model *do*:

        * An early version ended with "modify the file or read a different line range" —
          offering a *write* as the way to make a read succeed, which is how a stuck agent
          starts touching artifacts to defeat a cache.
        * Its replacement said "To force a fresh full read, request an explicit
          start_line/end_line range." True, and meant as an escape hatch — but thread
          `e3c15416` shows the model read it as *instructions*: one suppressed whole-file
          read of a 30KB rules file became **four** ranged reads (30-120, 120-250, 250-370,
          370-end), moving more bytes than the suppression saved.

        So: say the content is current, say where the bytes are, and offer the *cheap* next
        action (`op: get` for a single value). Do not document how to defeat the cache —
        a documented bypass is a bypass that gets used.
        """
        where = f"The full payload from that read is on disk at {externalized} — read_file that path if you need it verbatim. " if externalized else "Scroll back to that earlier read for the content. "
        return (
            f"[read_file dedup] {path} is unchanged — byte-identical to what you already read earlier in this run; "
            "the payload is omitted rather than resent. " + where + "You already have this content, so re-reading it in "
            "pieces costs more than it saves. To check one value in a JSON file, use apply_json_patches with "
            '{"op": "get", "pointer": "/..."} instead of another read. '
            "Do NOT write to the file to work around this notice — the content you have is current."
        )

    # -- core ------------------------------------------------------------

    def _handle_read(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        if not isinstance(result, ToolMessage):
            return result
        path = self._requested_path(request)
        if path is None:
            return result
        content = result.content if isinstance(result.content, str) else None
        if not content or len(content) < self._config.min_chars:
            return result
        # Never cache a failure: one transient error would otherwise be echoed forever
        # as "unchanged".
        if _looks_like_error(content):
            return result

        key = self._cache_key(request, path, _content_hash(content))
        call_id = request.tool_call.get("id")
        with _cache_lock:
            first_call_id = _cache.get(key)
            seen = key in _cache
            if seen:
                _cache.move_to_end(key)
            else:
                # Remember WHICH call produced the payload, so a later hit can point at the
                # artifact that call's output was externalised to.
                _cache[key] = call_id if isinstance(call_id, str) else ""
                while len(_cache) > self._config.max_entries:
                    _cache.popitem(last=False)
        if not seen:
            return result

        # A reference is only cheaper than the payload if the payload is still reachable.
        # The notice tells the model to "scroll back to that earlier read" — but a
        # subagent whose context was compacted between the two reads no longer has that
        # message, and neither ``scroll back`` nor ``op: get`` can recover it. Thread
        # ``7512ebd2``: a judgment subagent was refused ``judgment-schema.md`` and
        # ``schema_example.json`` this way, said so out loud ("the dedup system thinks I
        # read these in this run, but I did not — this is a fresh context"), then invented
        # its own output schema and filename. Suppression is therefore conditional on the
        # payload surviving somewhere the model can actually reach: still in the
        # transcript, or externalised to disk.
        first_read, searchable = self._first_read_message(request, first_call_id)
        externalized = self._externalized_path(first_read)
        if searchable and first_read is None:
            logger.info("read_file dedup: first read of %s is no longer in context (compacted); passing payload through", path)
            return result

        logger.info("read_file dedup: returning reference for %s", path)
        return ToolMessage(
            content=self._reference(path, externalized),
            tool_call_id=result.tool_call_id,
            name=result.name,
            # The read-before-write mark is carried over on purpose: it is derived from a
            # fresh disk read, not from this message body, so it still describes the current
            # version. Dropping it would force a re-read of content the model already has —
            # the exact waste this middleware exists to remove.
            additional_kwargs={**(result.additional_kwargs or {}), "read_file_dedup": True},
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if not self._config.enabled:
            return handler(request)
        name = request.tool_call.get("name")
        if name in _WRITE_TOOLS:
            result = handler(request)
            path = self._requested_path(request)
            if path is not None:
                self._invalidate_path(request, path)
            return result
        if name in _READ_TOOLS:
            return self._handle_read(request, handler(request))
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if not self._config.enabled:
            return await handler(request)
        name = request.tool_call.get("name")
        if name in _WRITE_TOOLS:
            result = await handler(request)
            path = self._requested_path(request)
            if path is not None:
                self._invalidate_path(request, path)
            return result
        if name in _READ_TOOLS:
            return self._handle_read(request, await handler(request))
        return await handler(request)


def build_read_file_dedup_middleware(config: Any = None) -> ReadFileDedupMiddleware | None:
    """Return the middleware only when enabled, so a disabled deployment pays nothing."""
    dedup_config = getattr(config, "read_file_dedup", None) if config is not None else None
    if not isinstance(dedup_config, ReadFileDedupConfig) or not dedup_config.enabled:
        return None
    return ReadFileDedupMiddleware(config=dedup_config)
