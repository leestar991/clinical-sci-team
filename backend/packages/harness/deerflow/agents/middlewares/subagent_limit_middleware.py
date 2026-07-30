"""Middleware to enforce maximum concurrent subagent tool calls per model response."""

import logging
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.subagents.executor import MAX_CONCURRENT_SUBAGENTS

logger = logging.getLogger(__name__)

# Valid range for max_concurrent_subagents
MIN_SUBAGENT_LIMIT = 2
MAX_SUBAGENT_LIMIT = 4

# Arguments the `task` tool cannot run without. A model that streams a partial
# tool call (or hallucinates an empty one) would otherwise dispatch a subagent
# with no instructions, which fails deep inside the executor instead of here.
REQUIRED_TASK_ARGS = ("description", "prompt", "subagent_type")


def _clamp_subagent_limit(value: int) -> int:
    """Clamp subagent limit to valid range [2, 4]."""
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


def _is_complete_task_call(tool_call: dict[str, Any]) -> bool:
    """Return True when a `task` tool call carries every required argument."""
    args = tool_call.get("args")
    if not isinstance(args, dict) or not args:
        return False

    for key in REQUIRED_TASK_ARGS:
        value = args.get(key)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if not isinstance(value, str) and not value:
            return False
    return True


def _is_task_call(tool_call: Any) -> bool:
    return isinstance(tool_call, dict) and tool_call.get("name") == "task"


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """Sanitizes 'task' tool calls emitted by a single model response.

    Two guards run in order:

    1. Incomplete `task` calls (missing ``description`` / ``prompt`` /
       ``subagent_type``) are dropped, since they cannot be dispatched.
    2. Remaining `task` calls beyond ``max_concurrent`` are truncated, keeping
       only the first ones. This is more reliable than prompt-based limits.

    Non-`task` tool calls are always preserved.

    Args:
        max_concurrent: Maximum number of concurrent subagent calls allowed.
            Defaults to MAX_CONCURRENT_SUBAGENTS (3). Clamped to [2, 4].
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_SUBAGENTS):
        super().__init__()
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)

    @staticmethod
    def _last_tool_calling_ai_message(state: AgentState) -> AIMessage | None:
        messages = state.get("messages") or []
        if not messages:
            return None

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None

        if not getattr(last_msg, "tool_calls", None):
            return None

        return last_msg

    @staticmethod
    def _drop_incomplete_task_calls(tool_calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Drop `task` calls missing required args. Returns (kept, dropped_count)."""
        kept = [tc for tc in tool_calls if not (_is_task_call(tc) and not _is_complete_task_call(tc))]
        return kept, len(tool_calls) - len(kept)

    def _cap_task_calls(self, tool_calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Keep only the first `max_concurrent` task calls. Returns (kept, dropped_count)."""
        task_indices = [i for i, tc in enumerate(tool_calls) if _is_task_call(tc)]
        if len(task_indices) <= self.max_concurrent:
            return tool_calls, 0

        indices_to_drop = set(task_indices[self.max_concurrent :])
        kept = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]
        return kept, len(indices_to_drop)

    def _sanitize_task_calls(self, state: AgentState) -> dict | None:
        last_msg = self._last_tool_calling_ai_message(state)
        if last_msg is None:
            return None

        tool_calls = list(last_msg.tool_calls)
        kept, incomplete_dropped = self._drop_incomplete_task_calls(tool_calls)
        kept, excess_dropped = self._cap_task_calls(kept)

        if not incomplete_dropped and not excess_dropped:
            return None

        if incomplete_dropped:
            logger.warning(f"Dropped {incomplete_dropped} incomplete task tool call(s) missing one of {REQUIRED_TASK_ARGS}")
        if excess_dropped:
            logger.warning(f"Truncated {excess_dropped} excess task tool call(s) from model response (limit: {self.max_concurrent})")

        # Replace the AIMessage with sanitized tool_calls (same id triggers replacement)
        return {"messages": [clone_ai_message_with_tool_calls(last_msg, kept)]}

    def _truncate_task_calls(self, state: AgentState) -> dict | None:
        """Apply only the concurrency cap, leaving incomplete calls untouched."""
        last_msg = self._last_tool_calling_ai_message(state)
        if last_msg is None:
            return None

        kept, excess_dropped = self._cap_task_calls(list(last_msg.tool_calls))
        if not excess_dropped:
            return None

        logger.warning(f"Truncated {excess_dropped} excess task tool call(s) from model response (limit: {self.max_concurrent})")
        return {"messages": [clone_ai_message_with_tool_calls(last_msg, kept)]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sanitize_task_calls(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sanitize_task_calls(state)
