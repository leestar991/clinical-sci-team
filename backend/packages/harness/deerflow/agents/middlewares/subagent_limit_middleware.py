"""Middleware to enforce maximum concurrent subagent tool calls per model response."""

import logging
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
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
#
# ``prompt`` has two accepted shapes in ``task_tool``: inline ``prompt`` or the
# path form ``prompt_file`` (the official rendered-template form — the lead is
# told to render with ``render_judge_prompt.py`` and pass ``prompt_file``).
# Requiring only ``prompt`` silently ate every prompt_file-based judgment
# dispatch as "incomplete" (f9231297: all 5 judgment batches logged a drop
# warning; the 3-slot error-feedback budget was consumed by the first wave,
# after which the same calls executed with the warning still emitted).
_SCALAR_REQUIRED_TASK_ARGS = ("description", "subagent_type")
_PROMPT_ARG_KEYS = ("prompt", "prompt_file")

# Maximum number of times within a single run that incomplete task calls are
# fed back as errors before the middleware gives up and drops them silently.
# This prevents infinite retry loops when the model is in a degraded state
# (e.g. context explosion) and cannot produce a complete task call.
_MAX_INCOMPLETE_ERROR_FEEDBACK = 3


def _clamp_subagent_limit(value: int) -> int:
    """Clamp subagent limit to valid range [2, 4]."""
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


def _arg_present(value: Any) -> bool:
    """A value counts as present when non-None and non-empty."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if not isinstance(value, str) and not value:
        return False
    return True


def _missing_task_args(tool_call: dict[str, Any]) -> list[str]:
    """Which required arguments a ``task`` call lacks (for the error message)."""
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    missing = [key for key in _SCALAR_REQUIRED_TASK_ARGS if not _arg_present(args.get(key))]
    if not any(_arg_present(args.get(key)) for key in _PROMPT_ARG_KEYS):
        missing.append("/".join(_PROMPT_ARG_KEYS))
    return missing


def _is_complete_task_call(tool_call: dict[str, Any]) -> bool:
    """Return True when a `task` tool call carries every required argument.

    The instruction arrives as EITHER inline ``prompt`` OR ``prompt_file``;
    requiring only ``prompt`` misclassified the official rendered-template
    form (``prompt_file``) as incomplete (root cause of f9231297's "silent"
    judgment-dispatch drops).
    """
    return not _missing_task_args(tool_call)


def _is_task_call(tool_call: Any) -> bool:
    return isinstance(tool_call, dict) and tool_call.get("name") == "task"


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """Sanitizes 'task' tool calls emitted by a single model response.

    Two guards run in order:

    1. Incomplete `task` calls (missing ``description`` / ``prompt`` /
       ``subagent_type``) are dropped.  For the first
       `_MAX_INCOMPLETE_ERROR_FEEDBACK` occurrences per run, error
       `ToolMessage`\s are injected so the model can self-correct.
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

    def _sanitize_task_calls(self, state: AgentState, runtime: Runtime | None = None) -> dict | None:
        last_msg = self._last_tool_calling_ai_message(state)
        if last_msg is None:
            return None

        tool_calls = list(last_msg.tool_calls)

        # Count how many incomplete feedbacks have already been sent this run
        incomplete_feedback_count: int = 0
        if runtime is not None:
            ctx = getattr(runtime, "context", None)
            if isinstance(ctx, dict):
                incomplete_feedback_count = ctx.get("_incomplete_task_feedback_count", 0)

        # Separate incomplete calls from complete ones
        incomplete_calls: list[dict[str, Any]] = []
        complete_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            if _is_task_call(tc) and not _is_complete_task_call(tc):
                incomplete_calls.append(tc)
            else:
                complete_calls.append(tc)

        # Cap complete task calls
        kept, excess_dropped = self._cap_task_calls(complete_calls)

        if not incomplete_calls and not excess_dropped:
            return None

        result_messages: list = []

        if incomplete_calls:
            logger.warning(f"Dropped {len(incomplete_calls)} incomplete task tool call(s) missing one of {*_SCALAR_REQUIRED_TASK_ARGS, '/'.join(_PROMPT_ARG_KEYS)}")
            # Inject error ToolMessages for incomplete calls so the model gets
            # a chance to self-correct.  Stop after _MAX_INCOMPLETE_ERROR_FEEDBACK
            # to prevent infinite loops when the model is degraded.
            if incomplete_feedback_count < _MAX_INCOMPLETE_ERROR_FEEDBACK:
                for tc in incomplete_calls:
                    missing = _missing_task_args(tc) or ["(unknown)"]
                    result_messages.append(
                        ToolMessage(
                            content=(f"task 调用缺少必需参数 {', '.join(missing)}，请补全后重发。当前调用已被丢弃，不计入并发预算。"),
                            tool_call_id=tc["id"],
                        )
                    )
                # Track the feedback count in the runtime context
                if runtime is not None:
                    ctx = getattr(runtime, "context", None)
                    if isinstance(ctx, dict):
                        ctx["_incomplete_task_feedback_count"] = incomplete_feedback_count + len(incomplete_calls)
            # Keep incomplete calls in the AIMessage so their tool_call_ids
            # match the error ToolMessages we just injected.
            kept = kept + incomplete_calls

        if excess_dropped:
            logger.warning(f"Truncated {excess_dropped} excess task tool call(s) from model response (limit: {self.max_concurrent})")

        # Task-call tracing aid: log which task calls survived the guards so the
        # gateway log alone can reconcile "model emitted" vs "tools executed"
        # without a database (the f9231297 drop investigation had to join three
        # stores to learn that these calls never reached the tool node).
        surviving = [(tc.get("id"), (tc.get("args") or {}).get("description")) for tc in kept if _is_task_call(tc)]
        if surviving:
            logger.debug("task calls surviving guards: %s", surviving)

        # Replace the AIMessage with sanitized tool_calls (same id triggers replacement)
        result_messages.append(clone_ai_message_with_tool_calls(last_msg, kept))
        return {"messages": result_messages}

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
        return self._sanitize_task_calls(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sanitize_task_calls(state, runtime)
