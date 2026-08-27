"""Tool error handling middleware and shared runtime middleware builders."""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.app_config import AppConfig
from deerflow.subagents.status_contract import (
    extract_subagent_status,
    make_subagent_additional_kwargs,
)

if TYPE_CHECKING:
    from deerflow.tools.builtins.tool_search import DeferredToolSetup

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_TASK_TOOL_NAME = "task"


def _stamp_task_subagent_status(message: ToolMessage, *, tool_name: str, error: str | None = None) -> ToolMessage:
    """Centralised stamping of ``additional_kwargs.subagent_status``.

    Bytedance/deer-flow issue #3146: the frontend now reads the subagent
    status from a structured field instead of parsing the leading text of
    the task tool's return string. That contract is enforced here, in the
    one place every task tool result flows through, rather than at the 5
    normal-return + 3 ``Error:`` pre-execution branches inside
    ``task_tool.py``. Centralisation prevents the "added a new return
    path, forgot the stamp" drift mode.

    For non-``task`` tools this is a no-op so other tools' additional_kwargs
    conventions are untouched.
    """
    if tool_name != _TASK_TOOL_NAME:
        return message
    content = message.content if isinstance(message.content, str) else ""
    status = extract_subagent_status(content)
    if status is None:
        # Non-terminal streaming chunks or unrecognised shapes leave the
        # field unset so the frontend can keep the card on its in-progress
        # placeholder until a real terminal frame arrives.
        return message
    stamp = make_subagent_additional_kwargs(status, error=error)
    existing = dict(message.additional_kwargs or {})
    existing.update(stamp)
    message.additional_kwargs = existing
    return message


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """Convert tool exceptions into error ToolMessages so the run can continue."""

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."

        content = f"Error: Tool '{tool_name}' failed with {exc.__class__.__name__}: {detail}. Continue with available context, or choose an alternative tool."
        message = ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )
        # Stamp the structured subagent status on the wrapper too: the
        # frontend would otherwise have to fall back to prefix-matching
        # ``Error: Tool 'task' failed ...`` on the wire. The ``subagent_error``
        # carries the same ``ExcClass: detail`` shape the wrapper string
        # uses so debugging artifacts stay aligned.
        structured_error = f"{exc.__class__.__name__}: {detail}"
        return _stamp_task_subagent_status(message, tool_name=tool_name, error=structured_error)

    @staticmethod
    def _maybe_stamp(result: ToolMessage | Command, request: ToolCallRequest) -> ToolMessage | Command:
        """Apply the subagent stamp to successful task tool returns.

        ``Command`` results bypass the stamp — they encode LangGraph
        control flow rather than user-facing tool output.
        """
        if not isinstance(result, ToolMessage):
            return result
        tool_name = str(request.tool_call.get("name") or "")
        return _stamp_task_subagent_status(result, tool_name=tool_name)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            result = handler(request)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception as exc:
            logger.exception("Tool execution failed (sync): name=%s id=%s", request.tool_call.get("name"), request.tool_call.get("id"))
            return self._build_error_message(request, exc)
        return self._maybe_stamp(result, request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            result = await handler(request)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception as exc:
            logger.exception("Tool execution failed (async): name=%s id=%s", request.tool_call.get("name"), request.tool_call.get("id"))
            return self._build_error_message(request, exc)
        return self._maybe_stamp(result, request)


def _build_runtime_middlewares(
    *,
    app_config: AppConfig,
    include_uploads: bool,
    include_dangling_tool_call_patch: bool,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """Build shared base middlewares for agent execution."""
    from deerflow.agents.middlewares.input_sanitization_middleware import InputSanitizationMiddleware
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
    from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
    from deerflow.agents.middlewares.tool_output_budget_middleware import ToolOutputBudgetMiddleware
    from deerflow.sandbox.middleware import SandboxMiddleware

    # Layer 1 — outermost wrap_model_call wrappers (listed outer→inner).
    # InputSanitizationMiddleware is first so it becomes the outermost
    # wrapper — sanitised messages are what every inner middleware sees.
    outer_wrappers: list[AgentMiddleware] = [
        InputSanitizationMiddleware(),
        ToolOutputBudgetMiddleware.from_app_config(app_config),
    ]

    # Layer 2 — before_agent hooks that read/annotate thread-scoped data.
    thread_hooks: list[AgentMiddleware] = [
        ThreadDataMiddleware(lazy_init=lazy_init),
    ]
    if include_uploads:
        from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware

        thread_hooks.append(UploadsMiddleware())
    thread_hooks.append(SandboxMiddleware(lazy_init=lazy_init))

    # Layer 3 — post-processing append-only middlewares.
    tail: list[AgentMiddleware] = []
    if include_dangling_tool_call_patch:
        from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

        tail.append(DanglingToolCallMiddleware())
    tail.append(LLMErrorHandlingMiddleware(app_config=app_config))

    # Guardrail middleware (if configured)
    guardrails_config = app_config.guardrails
    if guardrails_config.enabled and guardrails_config.provider:
        import inspect

        from deerflow.guardrails.middleware import GuardrailMiddleware
        from deerflow.reflection import resolve_variable

        provider_cls = resolve_variable(guardrails_config.provider.use)
        provider_kwargs = dict(guardrails_config.provider.config) if guardrails_config.provider.config else {}
        # Pass framework hint if the provider accepts it (e.g. for config discovery).
        # Built-in providers like AllowlistProvider don't need it, so only inject
        # when the constructor accepts 'framework' or '**kwargs'.
        if "framework" not in provider_kwargs:
            try:
                sig = inspect.signature(provider_cls.__init__)
                if "framework" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    provider_kwargs["framework"] = "deerflow"
            except (ValueError, TypeError):
                pass
        provider = provider_cls(**provider_kwargs)
        tail.append(GuardrailMiddleware(provider, fail_closed=guardrails_config.fail_closed, passport=guardrails_config.passport))

    from deerflow.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware

    tail.append(SandboxAuditMiddleware())

    if app_config.read_before_write.enabled:
        from deerflow.agents.middlewares.read_before_write_middleware import ReadBeforeWriteMiddleware

        tail.append(ReadBeforeWriteMiddleware())

    # Ordered BEFORE read_file_dedup: a blocked repeat whole-file read must not reach the
    # sandbox at all (the point is to not move the bytes), and dedup's bookkeeping should
    # only ever see reads that actually happened. Both are wrap_tool_call-only, so neither
    # adds a graph node — the recursion_limit/turn ratio recorded in config.yaml is
    # unaffected. Nothing is constructed when disabled.
    if app_config.read_file_policy.enabled:
        from deerflow.agents.middlewares.read_file_policy_middleware import ReadFilePolicyMiddleware

        tail.append(ReadFilePolicyMiddleware(config=app_config.read_file_policy))

    # Same placement rationale: refuse before the command runs, and stay a wrap_tool_call
    # so no graph node is added. Deliberately NOT in sandbox/tools.py's bash validators —
    # those run only for the local sandbox, leaving AIO/container deployments uncovered.
    if app_config.bash_write_policy.enabled:
        from deerflow.agents.middlewares.bash_write_policy_middleware import BashWritePolicyMiddleware

        tail.append(BashWritePolicyMiddleware(config=app_config.bash_write_policy))

    # Ordered AFTER read-before-write so this middleware sits closer to the tool and hashes
    # the real payload rather than something another middleware already rewrote.
    #
    # It is NOT ordered this way to protect the read mark: that mark is re-read from disk
    # (`ReadBeforeWriteMiddleware._attach_read_mark` calls its `_content_reader`), so it is
    # independent of the message body and a dedup reference carries a valid one either way —
    # deliberately, since dropping it would force a re-read of content the model already has.
    # Nothing is constructed when disabled, so a deployment that does not opt in pays no cost
    # and sees no behaviour change.
    if app_config.read_file_dedup.enabled:
        from deerflow.agents.middlewares.read_file_dedup_middleware import ReadFileDedupMiddleware

        tail.append(ReadFileDedupMiddleware(config=app_config.read_file_dedup))

    tail.append(ToolErrorHandlingMiddleware())

    return [*outer_wrappers, *thread_hooks, *tail]


def build_lead_runtime_middlewares(*, app_config: AppConfig, lazy_init: bool = True) -> list[AgentMiddleware]:
    """Middlewares shared by lead agent runtime before lead-only middlewares."""
    return _build_runtime_middlewares(
        app_config=app_config,
        include_uploads=True,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )


def build_subagent_runtime_middlewares(
    *,
    app_config: AppConfig | None = None,
    model_name: str | None = None,
    lazy_init: bool = True,
    deferred_setup: "DeferredToolSetup | None" = None,
) -> list[AgentMiddleware]:
    """Middlewares shared by subagent runtime before subagent-only middlewares."""
    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()

    middlewares = _build_runtime_middlewares(
        app_config=app_config,
        include_uploads=False,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )

    if model_name is None and app_config.models:
        model_name = app_config.models[0].name

    model_config = app_config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

        middlewares.append(ViewImageMiddleware())

    # Hide deferred (MCP) tool schemas from the subagent's model binding until
    # tool_search promotes them. This is the same wiring the lead agent gets. The deferred
    # set + catalog hash come from the build-time setup (assembled after
    # tool-policy filtering); promotion is read from graph state. Empty/None
    # setup (deferral disabled or no MCP tool survived) is a pure no-op.
    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

        middlewares.append(DeferredToolFilterMiddleware(deferred_setup.deferred_names, deferred_setup.catalog_hash))

    # ── Subagent runtime guards (Phase 1) ────────────────────────────────────
    # The lead agent has had all three of these from the start; the subagent chain had
    # none, which is how a single judgment task reached 6.36M tokens over 83 steps with
    # nobody compacting its context, capping its spend, or breaking its gate-script loop.
    # All three are opt-in so an untouched deployment keeps the exact chain it had.
    subagents_config = app_config.subagents

    if subagents_config.loop_detection.enabled and app_config.loop_detection.enabled:
        from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware

        # Thresholds come from the global section; the counting mode and the
        # mutation-aware reset differ — a subagent's repeats are spaced wider than the
        # 20-call window (reads and greps in between), so window-only counting never
        # reaches the limit; and its fix->verify cycles re-issue identical commands after
        # each mutation, which the reset recognizes as re-checks rather than loops.
        middlewares.append(
            LoopDetectionMiddleware.from_config(
                app_config.loop_detection,
                cumulative_counting=subagents_config.loop_detection.cumulative_counting,
                mutation_reset=subagents_config.loop_detection.mutation_reset,
            )
        )

    if subagents_config.summarization.enabled:
        from deerflow.agents.middlewares.summarization_middleware import build_summarization_middleware

        summarization = build_summarization_middleware(subagents_config.summarization, app_config=app_config)
        if summarization is not None:
            middlewares.append(summarization)

    if subagents_config.token_budget.enabled:
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        middlewares.append(TokenBudgetMiddleware.from_config(subagents_config.token_budget))

    # Same provider safety-termination guard the lead agent uses — subagents
    # are equally exposed to truncated tool_calls returned with
    # finish_reason=content_filter (and friends), and the bad call would then
    # propagate back to the lead agent via the task tool result.
    safety_config = app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware

        middlewares.append(SafetyFinishReasonMiddleware.from_config(safety_config))

    return middlewares
