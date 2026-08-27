"""Bound repeated whole-file reads of large files within one task.

Why this exists, and why ``ReadFileDedupMiddleware`` cannot cover it: dedup keys on
``(path, start_line, end_line, content_hash)``, so "read the whole thing, then read the
whole thing again, then read ranges of it" produces a different key almost every time —
every payload is legitimately *new* to the cache. On thread ``88df83a8`` dedup could only
ever hit once (``dedupable_read_calls=1``) while the EX judgment task performed **10**
whole-file reads, six of them on the same 7,604-line ``ocr_records.md``
(``range_overlap_lines=936``).

The cost is not the bytes of one read; it is that each read is re-inherited by every
following turn. That task's ``tokens_before`` reached 99,755 (1.66x the 60k compaction
trigger), compaction fired four times inside a single task, and the subagent then lost its
working state and rewrote its own goal — producing a self-invented ``qc_review_report.json``
instead of the judgment draft it was dispatched to write.

The delegation template already said "read each input at most once in this task". It was
violated six times in that one task, which is the argument for making it mechanical rather
than adding another sentence of prose.

Scope discipline (each bound exists to avoid making things worse):
* Only ``read_file``, only reads **without** a line range. A ranged read is the behaviour
  this middleware is steering *towards* and is never blocked.
* Only paths whose first whole-file read was at least ``min_lines_for_ranged`` lines.
  Blocking small files would add turns without saving context.
* Keyed per ``task_id``: subagent contexts are isolated, so task B's first read must not be
  refused because task A read the same file.
* An errored first read is not recorded — otherwise one transient failure would refuse the
  file for the rest of the task.
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.read_file_policy_config import ReadFilePolicyConfig

logger = logging.getLogger(__name__)

_READ_TOOLS = frozenset({"read_file"})

# ``read_file`` prefixes ranged/truncated output with this marker, which carries the real
# total line count even when the body was truncated by
# ``sandbox.read_file_output_max_chars``. Counting newlines in the body would under-report
# exactly the biggest files — the ones this policy is about.
_LINES_MARKER = re.compile(r"^\[lines\s+\d+-\d+\s+of\s+(\d+)\]")

# Module-level so it survives across middleware instances within a process: the agent (and
# therefore its middleware chain) is cached and reused across runs. Same rationale and same
# key shape as ReadFileDedupMiddleware.
_first_whole_read: OrderedDict[tuple, int] = OrderedDict()
_lock = threading.Lock()


def _reset_read_file_policy_cache() -> None:
    """Test helper: drop all bookkeeping."""
    with _lock:
        _first_whole_read.clear()


def _looks_like_error(content: str) -> bool:
    return content.lstrip().startswith("Error:")


def _line_count(content: str) -> int:
    marker = _LINES_MARKER.match(content.lstrip())
    if marker:
        try:
            return int(marker.group(1))
        except ValueError:
            pass
    return content.count("\n") + 1


class ReadFilePolicyMiddleware(AgentMiddleware):
    """Refuse (or flag) a repeat whole-file ``read_file`` of a large file in one task."""

    def __init__(self, config: ReadFilePolicyConfig | None = None) -> None:
        super().__init__()
        self._config = config or ReadFilePolicyConfig()

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _context(request: ToolCallRequest) -> dict:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _args(request: ToolCallRequest) -> dict:
        args = request.tool_call.get("args") or {}
        return args if isinstance(args, dict) else {}

    def _requested_path(self, request: ToolCallRequest) -> str | None:
        path = self._args(request).get("path")
        return path if isinstance(path, str) and path else None

    def _is_whole_file_read(self, request: ToolCallRequest) -> bool:
        args = self._args(request)
        return args.get("start_line") is None and args.get("end_line") is None

    def _key(self, request: ToolCallRequest, path: str) -> tuple:
        context = self._context(request)
        return (
            context.get("sandbox_id"),
            context.get("thread_id"),
            context.get("run_id"),
            # The isolation boundary: a subagent's transcript does not contain another
            # task's reads, so per-task bookkeeping is what keeps the first read honest.
            context.get("task_id"),
            posixpath.normpath(path),
        )

    def _guidance(self, path: str, line_count: int) -> str:
        """What the model should do instead.

        Written as a concrete substitution, not a prohibition: a refusal without a cheaper
        path is how an agent starts inventing workarounds (thread ``e3c15416``: a
        suppressed whole-file read turned into four ranged reads that moved more bytes than
        the suppression saved). So name the two tools that answer "where is X" without
        re-sending the file.
        """
        return (
            f"{path} was already read in full in this task ({line_count} lines). "
            "Re-reading it whole re-sends the same content and it is then re-sent again on every "
            "following turn, which is what exhausts the context window. "
            "Locate what you need instead: grep(path, pattern) for the line numbers, then "
            "read_file(path, start_line=..., end_line=...) for that section only. "
            'For a single value in a JSON file use apply_json_patches with {"op": "get", "pointer": "/..."}. '
            "Scroll back to the earlier read for content you already have."
        )

    # -- core ------------------------------------------------------------

    def _record(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        """Remember the size of a successful first whole-file read."""
        if not isinstance(result, ToolMessage):
            return result
        content = result.content if isinstance(result.content, str) else None
        # A failed read is not a read: recording it would refuse the file for the rest of
        # the task over one transient error.
        if not content or _looks_like_error(content):
            return result

        path = self._requested_path(request)
        if path is None:
            return result

        key = self._key(request, path)
        with _lock:
            if key not in _first_whole_read:
                _first_whole_read[key] = _line_count(content)
                while len(_first_whole_read) > self._config.max_entries:
                    _first_whole_read.popitem(last=False)
        return result

    def _governed_line_count(self, request: ToolCallRequest, path: str) -> int | None:
        """Line count of this path's earlier whole-file read, if the policy governs it."""
        key = self._key(request, path)
        with _lock:
            line_count = _first_whole_read.get(key)
            if line_count is not None:
                _first_whole_read.move_to_end(key)
        if line_count is None or line_count < self._config.min_lines_for_ranged:
            return None
        return line_count

    def _applies(self, request: ToolCallRequest) -> str | None:
        """Return the path when this call is a governed repeat whole-file read."""
        if not self._config.enabled:
            return None
        if request.tool_call.get("name") not in _READ_TOOLS:
            return None
        if not self._is_whole_file_read(request):
            return None
        return self._requested_path(request)

    def _refusal(self, request: ToolCallRequest, path: str, line_count: int) -> ToolMessage:
        logger.info("read_file policy: blocked repeat whole-file read of %s (%d lines)", path, line_count)
        return ToolMessage(
            content="Error: " + self._guidance(path, line_count),
            tool_call_id=request.tool_call.get("id") or "",
            name="read_file",
            additional_kwargs={"read_file_policy": "blocked"},
        )

    def _annotate(self, request: ToolCallRequest, result: ToolMessage | Command, path: str, line_count: int) -> ToolMessage | Command:
        if not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            return result
        logger.info("read_file policy: flagged repeat whole-file read of %s (%d lines)", path, line_count)
        return ToolMessage(
            content=f"{result.content}\n\n[read_file policy] {self._guidance(path, line_count)}",
            tool_call_id=result.tool_call_id,
            name=result.name,
            additional_kwargs={**(result.additional_kwargs or {}), "read_file_policy": "warned"},
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        path = self._applies(request)
        if path is None:
            return handler(request)

        line_count = self._governed_line_count(request, path)
        if line_count is None:
            return self._record(request, handler(request))
        if self._config.mode == "block":
            # Returned without calling the handler: the point is to not move the bytes.
            return self._refusal(request, path, line_count)
        return self._annotate(request, handler(request), path, line_count)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        path = self._applies(request)
        if path is None:
            return await handler(request)

        line_count = self._governed_line_count(request, path)
        if line_count is None:
            return self._record(request, await handler(request))
        if self._config.mode == "block":
            return self._refusal(request, path, line_count)
        return self._annotate(request, await handler(request), path, line_count)


def build_read_file_policy_middleware(config: ReadFilePolicyConfig | None = None) -> ReadFilePolicyMiddleware | None:
    """Return the middleware only when enabled, so a disabled deployment pays nothing."""
    if config is None or not config.enabled:
        return None
    return ReadFilePolicyMiddleware(config=config)
