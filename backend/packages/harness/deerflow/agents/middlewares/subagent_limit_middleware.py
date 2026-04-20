"""Middleware to enforce maximum concurrent subagent tool calls per model response."""

import logging
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.subagents.executor import MAX_CONCURRENT_SUBAGENTS

logger = logging.getLogger(__name__)

# Valid range for max_concurrent_subagents
MIN_SUBAGENT_LIMIT = 2
MAX_SUBAGENT_LIMIT = 4

# Required fields for a well-formed task() tool call
_TASK_REQUIRED_FIELDS = frozenset({"description", "prompt", "subagent_type"})


def _clamp_subagent_limit(value: int) -> int:
    """Clamp subagent limit to valid range [2, 4]."""
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


def _is_complete_task_call(tc: dict) -> bool:
    """Return True if a task() tool call has all required non-empty fields."""
    args = tc.get("args") or {}
    return all(bool(args.get(f)) for f in _TASK_REQUIRED_FIELDS)


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """Validates and limits 'task' tool calls from a single model response.

    Two behaviours are applied in order:

    1. **Drop incomplete calls** — Any ``task`` call whose ``args`` dict is
       missing or has empty values for ``description``, ``prompt``, or
       ``subagent_type`` is silently removed before tool execution.  This
       prevents the "Field required" error from polluting the message chain and
       avoids a wasted recovery turn.

    2. **Truncate excess calls** — After incomplete calls are removed, if more
       than ``max_concurrent`` valid ``task`` calls remain, only the first
       ``max_concurrent`` are kept and the rest are discarded.

    Args:
        max_concurrent: Maximum number of concurrent subagent calls allowed.
            Defaults to MAX_CONCURRENT_SUBAGENTS (3). Clamped to [2, 4].
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_SUBAGENTS):
        super().__init__()
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)

    def _sanitize_task_calls(self, state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
        if not task_indices:
            return None

        # Step 1: identify incomplete task calls
        incomplete = {i for i in task_indices if not _is_complete_task_call(tool_calls[i])}
        if incomplete:
            logger.warning(
                "Dropping %d task call(s) with missing required fields (description/prompt/subagent_type)",
                len(incomplete),
            )

        # Step 2: from the remaining valid calls, apply the concurrency cap
        valid_task_indices = [i for i in task_indices if i not in incomplete]
        excess = set(valid_task_indices[self.max_concurrent :])
        if excess:
            logger.warning(
                "Truncated %d excess task tool call(s) from model response (limit: %d)",
                len(excess),
                self.max_concurrent,
            )

        indices_to_drop = incomplete | excess
        if not indices_to_drop:
            return None

        truncated = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]
        updated_msg = last_msg.model_copy(update={"tool_calls": truncated})
        return {"messages": [updated_msg]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sanitize_task_calls(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sanitize_task_calls(state)
