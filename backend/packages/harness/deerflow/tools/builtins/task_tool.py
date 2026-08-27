"""Task tool for delegating work to subagents."""

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain.tools import InjectedToolCallId, tool
from langchain_core.callbacks import BaseCallbackManager
from langgraph.config import get_stream_writer

from deerflow.config import get_app_config
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.security import LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE, is_host_bash_allowed
from deerflow.subagents import SubagentExecutor, get_available_subagent_names, get_subagent_config
from deerflow.subagents.config import resolve_subagent_model_name
from deerflow.subagents.executor import (
    SubagentStatus,
    cleanup_background_task,
    get_background_task_result,
    request_cancel_background_task,
)
from deerflow.tools.types import Runtime
from deerflow.trace_context import DEERFLOW_TRACE_METADATA_KEY, get_current_trace_id, normalize_trace_id

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# Maximum number of automatic retries when a subagent task FAILS. Timeouts and
# user cancellations are never retried: a timeout means the work was too big for
# the budget, so re-running it would just burn the same budget again.
SUBAGENT_MAX_RETRIES = 1

# Maximum characters of the last AI message surfaced as a partial result when a
# subagent times out, so the lead agent can salvage completed work.
SUBAGENT_PARTIAL_RESULT_LIMIT = 4000

# ── prompt_file: hand the assignment over by path instead of by value ────────────
# Some delegation templates are contracts that must reach the subagent **verbatim** —
# `eligibility-judgment`'s judge-delegation template is ~12.7k chars, and paraphrasing it
# has a failure of record (session `9a83ccc9`: the lead re-told it in 1.8k chars, dropped
# one of the gate commands, and the subagent invented its own output schema).
#
# Requiring the lead to type it out satisfies verbatimness but bills for it. Session
# `247a535f`: the three-way judgment dispatch was the single slowest lead call of the
# session — 143.6s and 15,265 output tokens for one AIMessage carrying three ~7.5k-char
# prompts. The lead's own reasoning was 56% of its output tokens across the run, so this
# is the dominant lead-side cost, and it buys nothing: the text is a fixed template.
#
# With `prompt_file` the template is rendered to a file mechanically (by a skill script)
# and the lead passes a path. That is *more* faithful than hand-copying, not less: no
# model ever re-emits the bytes. `prompt` stays supported and unchanged for the ordinary
# ad-hoc case.
#
# Same prefix restriction as `expected_outputs`, for the same reason: a path outside
# user-data would turn delegation into a host-file read. Skills are readable inputs but
# are deliberately excluded — a prompt is a rendered artifact, and pointing at a skill
# file would send the *template* (placeholders unresolved) rather than the assignment.
PROMPT_FILE_PREFIX = "/mnt/user-data/"
# A prompt is an assignment, not a payload. The largest real template is ~12.7k chars;
# 200k leaves room for far bigger ones while still refusing "the lead pointed at an OCR
# dump", which would blow the subagent's context on its very first message.
PROMPT_FILE_MAX_CHARS = 200_000

# Cache subagent token usage by tool_call_id so TokenUsageMiddleware can
# write it back to the triggering AIMessage's usage_metadata.
_subagent_usage_cache: dict[str, dict[str, int]] = {}

# ── expected_outputs: mechanical post-condition on a subagent's artifacts ────────
# A subagent reporting COMPLETED is a self-assessment, and thread `88df83a8` showed what
# that is worth: the EX judgment subagent never wrote
# `judgments_draft_MCRC-2150006_EX.json`, wrote a self-invented `qc_review_report.json`
# instead, and returned `completed`. The lead only found out 8 minutes later by running
# the structure gate itself.
#
# Declared paths must live under the sandbox's user-data prefix. Two reasons, both hard:
# a declaration pointing at the host filesystem would turn a completion check into a host
# probe, and anything outside user-data is not a task artifact (skills are read-only
# inputs, `/tmp` is not delivered).
EXPECTED_OUTPUTS_PREFIX = "/mnt/user-data/"
EXPECTED_OUTPUTS_LIMIT = 10
# Marker in the failure message. Kept greppable so offline analysis can separate
# "the subagent produced nothing" from ordinary failures.
EXPECTED_OUTPUTS_FAILURE_MARKER = "required outputs missing/empty"


def _normalize_expected_outputs(declared: list[str] | None) -> tuple[list[str], str | None]:
    """Validate + de-duplicate an ``expected_outputs`` declaration.

    Returns ``(paths, error)``; exactly one side is populated. Validation runs at the tool
    boundary rather than inside the executor so a malformed declaration costs nothing —
    failing after the subagent ran would burn a whole task's allowance to report a typo.
    """
    if not declared:
        return [], None

    if len(declared) > EXPECTED_OUTPUTS_LIMIT:
        return [], f"Error: expected_outputs accepts at most {EXPECTED_OUTPUTS_LIMIT} paths, got {len(declared)}. Declare only the artifacts that make the task worthless if absent."

    seen: set[str] = set()
    paths: list[str] = []
    for entry in declared:
        if not isinstance(entry, str) or not entry.strip():
            return [], f"Error: expected_outputs entries must be non-empty absolute paths under {EXPECTED_OUTPUTS_PREFIX}, got {entry!r}."
        path = entry.strip()
        if not path.startswith(EXPECTED_OUTPUTS_PREFIX):
            return [], f"Error: expected_outputs path {path!r} must be an absolute path under {EXPECTED_OUTPUTS_PREFIX}."
        if ".." in path.split("/"):
            return [], f"Error: expected_outputs path {path!r} must not contain '..'; give the resolved path under {EXPECTED_OUTPUTS_PREFIX}."
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths, None


def _resolve_prompt_source(runtime: Runtime, prompt: str | None, prompt_file: str | None) -> tuple[str | None, str | None]:
    """Return ``(prompt_text, error)`` for the ``prompt`` / ``prompt_file`` pair.

    Exactly one side is populated. Validation and reading both happen at the tool
    boundary so a bad declaration costs no subagent allowance — the same rule
    ``_normalize_expected_outputs`` follows.
    """
    has_prompt = bool(prompt and prompt.strip())
    has_file = bool(prompt_file and prompt_file.strip())

    if has_prompt and has_file:
        return None, "Error: pass either prompt or prompt_file, not both. Use prompt_file when a skill script rendered the assignment to a file; use prompt for an inline assignment."
    if not has_prompt and not has_file:
        return None, "Error: task requires a prompt (inline) or prompt_file (path to a rendered assignment)."
    if has_prompt:
        return cast(str, prompt), None

    path = cast(str, prompt_file).strip()
    if not path.startswith(PROMPT_FILE_PREFIX):
        return None, f"Error: prompt_file {path!r} must be an absolute path under {PROMPT_FILE_PREFIX}."
    if ".." in path.split("/"):
        return None, f"Error: prompt_file {path!r} must not contain '..'; give the resolved path under {PROMPT_FILE_PREFIX}."

    try:
        from deerflow.sandbox.tools import read_current_file_content

        content = read_current_file_content(runtime, path)
    except FileNotFoundError:
        return None, f"Error: prompt_file not found: {path}. Render the assignment to that path first, then delegate."
    except Exception as e:  # noqa: BLE001 - surfaced to the model, never raised
        return None, f"Error: could not read prompt_file {path}: {e}"

    if not content or not content.strip():
        return None, f"Error: prompt_file {path} is empty. A subagent given an empty assignment would invent one."
    if len(content) > PROMPT_FILE_MAX_CHARS:
        return None, f"Error: prompt_file {path} is {len(content):,} chars, over the {PROMPT_FILE_MAX_CHARS:,} limit. A prompt is an assignment, not a data payload — pass data by path inside the prompt instead."
    return content, None


def _is_retryable_failure(stop_reason: str | None, app_config: "AppConfig | None") -> bool:
    """Whether ``task`` should spend a retry on this failure.

    A resource-ceiling failure (recursion/max_turns, token budget) is not retried by
    default, for exactly the reason timeouts never were: the work was too big for the
    allowance, so running it again spends the same allowance and fails the same way. In
    session ``d393714d`` the unconditional retry turned a 6.36M-token failure into
    11.57M. Deployments that want the old behaviour can set
    ``subagents.graceful_stop.retry_resource_ceiling_failures: true``.
    """
    from deerflow.subagents.stop_reasons import is_resource_ceiling

    if not is_resource_ceiling(stop_reason):
        return True
    config = app_config
    if config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
        except Exception:
            return False
    try:
        return bool(config.subagents.graceful_stop.retry_resource_ceiling_failures)
    except AttributeError:
        return False


# Cap on the failure text carried into a retry. The messages worth forwarding are the
# mechanical ones (artifact gate, tool errors); a stack trace or a dumped payload would
# push the actual assignment out of the model's attention.
_RETRY_REASON_CHAR_BUDGET = 1200


def _retry_prompt(prompt: str, error: str | None) -> str:
    """The original assignment plus why the previous attempt was rejected.

    A retry re-ran the byte-identical prompt, which means the second attempt could not
    know what the first one got wrong — and a subagent whose context is isolated has no
    other channel to learn it. Thread ``7512ebd2``: both judgment tasks failed the
    artifact gate for writing a self-invented filename (``judgment_IN.md`` /
    ``judgments_EX.json``) instead of the declared ``judgments_draft_{id}_{TRACK}.json``;
    each retry received the same prompt, made the same substitution, and failed the same
    gate. Four attempts, ~7.5M tokens, no artifact.

    Only the failure text is added — never a rewritten assignment. The prompt is the
    lead's contract with the subagent, and editing it here would silently compete with
    the delegation template that produced it.
    """
    if not error or not error.strip():
        return prompt
    reason = error.strip()
    if len(reason) > _RETRY_REASON_CHAR_BUDGET:
        reason = reason[:_RETRY_REASON_CHAR_BUDGET] + "\n…(truncated)"
    return (
        f"{prompt}\n\n"
        "⛔ **RETRY — the previous attempt at this exact task was REJECTED.** Reason reported by the "
        f"mechanical check:\n\n{reason}\n\n"
        "Fix that specific defect this time. The assignment above is unchanged and still authoritative: "
        "produce the declared artifacts at the declared paths, and do not substitute a file of your own naming."
    )


def _token_usage_cache_enabled(app_config: "AppConfig | None") -> bool:
    if app_config is None:
        try:
            app_config = get_app_config()
        except FileNotFoundError:
            return False
    return bool(getattr(getattr(app_config, "token_usage", None), "enabled", False))


def _cache_subagent_usage(tool_call_id: str, usage: dict | None, *, enabled: bool = True) -> None:
    if enabled and usage:
        _subagent_usage_cache[tool_call_id] = usage


def pop_cached_subagent_usage(tool_call_id: str) -> dict | None:
    return _subagent_usage_cache.pop(tool_call_id, None)


def _is_subagent_terminal(result: Any) -> bool:
    """Return whether a background subagent result is safe to clean up."""
    return result.status in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.CANCELLED, SubagentStatus.TIMED_OUT} or getattr(result, "completed_at", None) is not None


async def _await_subagent_terminal(task_id: str, max_polls: int) -> Any | None:
    """Poll until the background subagent reaches a terminal status or we run out of polls."""
    for _ in range(max_polls):
        result = get_background_task_result(task_id)
        if result is None:
            return None
        if _is_subagent_terminal(result):
            return result
        await asyncio.sleep(5)
    return None


async def _deferred_cleanup_subagent_task(task_id: str, trace_id: str, max_polls: int) -> None:
    """Keep polling a cancelled subagent until it can be safely removed."""
    cleanup_poll_count = 0
    while True:
        result = get_background_task_result(task_id)
        if result is None:
            return
        if _is_subagent_terminal(result):
            cleanup_background_task(task_id)
            return
        if cleanup_poll_count >= max_polls:
            logger.warning(f"[trace={trace_id}] Deferred cleanup for task {task_id} timed out after {cleanup_poll_count} polls")
            return
        await asyncio.sleep(5)
        cleanup_poll_count += 1


def _log_cleanup_failure(cleanup_task: asyncio.Task[None], *, trace_id: str, task_id: str) -> None:
    if cleanup_task.cancelled():
        return

    exc = cleanup_task.exception()
    if exc is not None:
        logger.error(f"[trace={trace_id}] Deferred cleanup failed for task {task_id}: {exc}")


def _schedule_deferred_subagent_cleanup(task_id: str, trace_id: str, max_polls: int) -> None:
    logger.debug(f"[trace={trace_id}] Scheduling deferred cleanup for cancelled task {task_id}")
    cleanup_task = asyncio.create_task(_deferred_cleanup_subagent_task(task_id, trace_id, max_polls))
    cleanup_task.add_done_callback(lambda task: _log_cleanup_failure(task, trace_id=trace_id, task_id=task_id))


def _find_usage_recorder(runtime: Any) -> Any | None:
    """Find a callback handler with ``record_external_llm_usage_records`` in the runtime config.

    LangChain may pass ``config["callbacks"]`` in three different shapes:

    - ``None`` (no callbacks registered): no recorder.
    - A plain ``list[BaseCallbackHandler]``: iterate it directly.
    - A ``BaseCallbackManager`` instance (e.g. ``AsyncCallbackManager`` on async
      tool runs): managers are not iterable, so we unwrap ``.handlers`` first.

    Any other shape (e.g. a single handler object accidentally passed without a
    list wrapper) cannot be iterated safely; treat it as "no recorder" rather
    than raise.
    """
    if runtime is None:
        return None
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    callbacks = config.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        callbacks = callbacks.handlers
    if not callbacks:
        return None
    if not isinstance(callbacks, list):
        return None
    for cb in callbacks:
        if hasattr(cb, "record_external_llm_usage_records"):
            return cb
    return None


def _summarize_usage(records: list[dict] | None) -> dict | None:
    """Summarize token usage records into a compact dict for SSE events."""
    if not records:
        return None
    return {
        "input_tokens": sum(r.get("input_tokens", 0) or 0 for r in records),
        "output_tokens": sum(r.get("output_tokens", 0) or 0 for r in records),
        "total_tokens": sum(r.get("total_tokens", 0) or 0 for r in records),
    }


def _report_subagent_usage(runtime: Any, result: Any) -> None:
    """Report subagent token usage to the parent RunJournal, if available.

    Each subagent task must be reported only once (guarded by usage_reported).
    """
    if getattr(result, "usage_reported", True):
        return
    records = getattr(result, "token_usage_records", None) or []
    if not records:
        return
    journal = _find_usage_recorder(runtime)
    if journal is None:
        logger.debug("No usage recorder found in runtime callbacks — subagent token usage not recorded")
        return
    try:
        journal.record_external_llm_usage_records(records)
        result.usage_reported = True
    except Exception:
        logger.warning("Failed to report subagent token usage", exc_info=True)


def _get_runtime_app_config(runtime: Any) -> "AppConfig | None":
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        app_config = context.get("app_config")
        if app_config is not None:
            return cast("AppConfig", app_config)
    return None


def _merge_skill_allowlists(parent: list[str] | None, child: list[str] | None) -> list[str] | None:
    """Return the effective subagent skill allowlist under the parent policy."""
    if parent is None:
        return child
    if child is None:
        return list(parent)

    parent_set = set(parent)
    return [skill for skill in child if skill in parent_set]


def _extract_partial_result(ai_messages: list[dict] | None) -> str:
    """Render the last AI message of a timed-out subagent as a partial result.

    A timeout still often carries most of the useful work. Returning it lets the
    lead agent continue from partial findings instead of discarding the run.
    Returns an empty string when there is nothing textual to salvage.
    """
    if not ai_messages:
        return ""

    last_msg = ai_messages[-1]
    content = last_msg.get("content", "") if isinstance(last_msg, dict) else getattr(last_msg, "content", "")

    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        text = "\n".join(block for block in blocks if block.strip())

    if not text.strip():
        return ""
    return f"\n\nPartial result (work completed before timeout):\n{text[:SUBAGENT_PARTIAL_RESULT_LIMIT]}"


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: Runtime,
    description: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    prompt: str | None = None,
    prompt_file: str | None = None,
    expected_outputs: list[str] | None = None,
) -> str:
    """Delegate a task to a specialized subagent that runs in its own context.

    Subagents help you:
    - Preserve context by keeping exploration and implementation separate
    - Handle complex multi-step tasks autonomously
    - Execute commands or operations in isolated contexts

    Built-in subagent types:
    - **general-purpose**: A capable agent for complex, multi-step tasks that require
      both exploration and action. Use when the task requires complex reasoning,
      multiple dependent steps, or would benefit from isolated context.
    - **bash**: Command execution specialist for running bash commands. This is only
      available when host bash is explicitly allowed or when using an isolated shell
      sandbox such as `AioSandboxProvider`.

    Additional custom subagent types may be defined in config.yaml under
    `subagents.custom_agents`. Each custom type can have its own system prompt,
    tools, skills, model, and timeout configuration. If an unknown subagent_type
    is provided, the error message will list all available types.

    When to use this tool:
    - Complex tasks requiring multiple steps or tools
    - Tasks that produce verbose output
    - When you want to isolate context from the main conversation
    - Parallel research or exploration tasks

    When NOT to use this tool:
    - Simple, single-step operations (use tools directly)
    - Tasks requiring user interaction or clarification

    Args:
        description: A short (3-5 word) description of the task for logging/display. ALWAYS PROVIDE THIS PARAMETER FIRST.
        subagent_type: The type of subagent to use. ALWAYS PROVIDE THIS PARAMETER SECOND.
        prompt: The task description for the subagent. Be specific and clear about what needs to be done. Provide either this or prompt_file, never both.
        prompt_file: Absolute path (under `/mnt/user-data/`) to a file holding the assignment;
            the subagent receives that file's contents as its prompt. Use it when a skill
            script rendered the assignment to a file, so a long fixed delegation template
            reaches the subagent verbatim without you retyping it.
        expected_outputs: Optional list of absolute file paths the subagent MUST have written
            for the task to count as done. Each path must be under `/mnt/user-data/` (at most 10).
            When any declared path is missing or empty, the task is reported as FAILED with the
            missing paths named, instead of trusting the subagent's own "completed" claim.
            Declare a path whenever the task's whole point is producing that file.
    """
    expected_output_paths, expected_outputs_error = _normalize_expected_outputs(expected_outputs)
    runtime_app_config = _get_runtime_app_config(runtime)
    cache_token_usage = _token_usage_cache_enabled(runtime_app_config)
    available_subagent_names = get_available_subagent_names(app_config=runtime_app_config) if runtime_app_config is not None else get_available_subagent_names()

    # Get subagent configuration
    config = get_subagent_config(subagent_type, app_config=runtime_app_config) if runtime_app_config is not None else get_subagent_config(subagent_type)
    if config is None:
        available = ", ".join(available_subagent_names)
        return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"
    if subagent_type == "bash":
        host_bash_allowed = is_host_bash_allowed(runtime_app_config) if runtime_app_config is not None else is_host_bash_allowed()
        if not host_bash_allowed:
            return f"Error: {LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE}"
    # Reported after the subagent-type checks so their (pre-existing, more fundamental)
    # errors keep priority, but still before any subagent is dispatched: a malformed
    # declaration must cost zero task allowance.
    if expected_outputs_error is not None:
        return expected_outputs_error

    resolved_prompt, prompt_error = _resolve_prompt_source(runtime, prompt, prompt_file)
    if prompt_error is not None:
        return prompt_error
    prompt = cast(str, resolved_prompt)

    # Build config overrides
    overrides: dict = {}

    # Skills are loaded by SubagentExecutor per-session (aligned with Codex's pattern:
    # each subagent loads its own skills based on config, injected as conversation items).
    # No longer appended to system_prompt here.

    # Extract parent context from runtime
    sandbox_state = None
    thread_data = None
    thread_id = None
    parent_model = None
    trace_id = None
    user_id = None
    deerflow_trace_id = None
    metadata: dict = {}

    if runtime is not None:
        sandbox_state = runtime.state.get("sandbox")
        thread_data = runtime.state.get("thread_data")
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            thread_id = runtime.config.get("configurable", {}).get("thread_id")

        # Try to get parent model from configurable
        metadata = runtime.config.get("metadata", {})
        parent_model = metadata.get("model_name")

        # Get or generate trace_id for distributed tracing
        trace_id = metadata.get("trace_id") or str(uuid.uuid4())[:8]

    # Get user_id for tracing (uses standard resolution order)
    user_id = resolve_runtime_user_id(runtime)

    # Propagate the authenticated runtime context so delegated tool calls are
    # evaluated by GuardrailMiddleware with the same identity/attribution as
    # the lead agent. Sourced from the server-side context written by
    # inject_authenticated_user_context (and run_id by the run worker); stays
    # None when absent (e.g. internal-auth runs) so guardrail behavior is
    # unchanged. Without this, role-aware policy silently mis-attributes any
    # tool call delegated to a subagent (user_role=None).
    parent_context = runtime.context if runtime is not None else None
    parent_context = parent_context if isinstance(parent_context, dict) else {}
    user_role = parent_context.get("user_role")
    oauth_provider = parent_context.get("oauth_provider")
    oauth_id = parent_context.get("oauth_id")
    run_id = parent_context.get("run_id")
    # Run-scoped RunJournal (written into the lead context by runtime/runs/worker.py).
    # Forwarded so middleware executing inside the subagent can persist its own
    # `middleware:*` audit events instead of silently dropping them.
    run_journal = parent_context.get("__run_journal")
    deerflow_trace_id = normalize_trace_id(parent_context.get(DEERFLOW_TRACE_METADATA_KEY)) or normalize_trace_id(metadata.get(DEERFLOW_TRACE_METADATA_KEY)) or get_current_trace_id()

    parent_available_skills = metadata.get("available_skills")
    if parent_available_skills is not None:
        overrides["skills"] = _merge_skill_allowlists(list(parent_available_skills), config.skills)

    if overrides:
        config = replace(config, **overrides)

    # Get available tools (excluding task tool to prevent nesting)
    # Lazy import to avoid circular dependency
    from deerflow.tools import get_available_tools

    # Inherit parent agent's tool_groups so subagents respect the same restrictions
    parent_tool_groups = metadata.get("tool_groups")
    resolved_app_config = runtime_app_config
    if config.model == "inherit" and parent_model is None and resolved_app_config is None:
        resolved_app_config = get_app_config()
    effective_model = resolve_subagent_model_name(config, parent_model, app_config=resolved_app_config)

    # Subagents should not have subagent tools enabled (prevent recursive nesting)
    available_tools_kwargs = {
        "model_name": effective_model,
        "groups": parent_tool_groups,
        "subagent_enabled": False,
    }
    if resolved_app_config is not None:
        available_tools_kwargs["app_config"] = resolved_app_config
    tools = get_available_tools(**available_tools_kwargs)

    # Create executor
    executor_kwargs = {
        "config": config,
        "tools": tools,
        "parent_model": parent_model,
        "sandbox_state": sandbox_state,
        "thread_data": thread_data,
        "thread_id": thread_id,
        "trace_id": trace_id,
        "user_id": user_id,
        "user_role": user_role,
        "oauth_provider": oauth_provider,
        "oauth_id": oauth_id,
        "run_id": run_id,
        "deerflow_trace_id": deerflow_trace_id,
        "run_journal": run_journal,
        # Part of executor_kwargs (not a call argument) so the retry branch below, which
        # rebuilds SubagentExecutor from the same dict, re-checks the same artifacts.
        "expected_outputs": expected_output_paths,
    }
    if resolved_app_config is not None:
        executor_kwargs["app_config"] = resolved_app_config
    executor = SubagentExecutor(**executor_kwargs)

    # Start background execution (always async to prevent blocking)
    # Use tool_call_id as task_id for better traceability
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # Poll for task completion in backend (removes need for LLM to poll)
    poll_count = 0
    last_status = None
    last_message_count = 0  # Track how many AI messages we've already sent
    # Polling timeout: execution timeout + 60s buffer, checked every 5s
    max_poll_count = (config.timeout_seconds + 60) // 5
    # Read the module global at call time so tests (and future config wiring)
    # can override the retry budget.
    retries_left = SUBAGENT_MAX_RETRIES
    attempt = 0  # 0-based attempt index, used to build unique task_ids on retry

    logger.info(f"[trace={trace_id}] Started background task {task_id} (subagent={subagent_type}, timeout={config.timeout_seconds}s, polling_limit={max_poll_count} polls)")

    writer = get_stream_writer()
    # Send Task Started message'
    writer({"type": "task_started", "task_id": task_id, "description": description})

    try:
        while True:
            result = get_background_task_result(task_id)

            if result is None:
                logger.error(f"[trace={trace_id}] Task {task_id} not found in background tasks")
                writer({"type": "task_failed", "task_id": task_id, "error": "Task disappeared from background tasks"})
                cleanup_background_task(task_id)
                return f"Error: Task {task_id} disappeared from background tasks"

            # Log status changes for debugging
            if result.status != last_status:
                logger.info(f"[trace={trace_id}] Task {task_id} status: {result.status.value}")
                last_status = result.status

            # Check for new AI messages and send task_running events
            ai_messages = result.ai_messages or []
            current_message_count = len(ai_messages)
            if current_message_count > last_message_count:
                # Send task_running event for each new message
                for i in range(last_message_count, current_message_count):
                    message = ai_messages[i]
                    writer(
                        {
                            "type": "task_running",
                            "task_id": task_id,
                            "message": message,
                            "message_index": i + 1,  # 1-based index for display
                            "total_messages": current_message_count,
                        }
                    )
                    logger.info(f"[trace={trace_id}] Task {task_id} sent message #{i + 1}/{current_message_count}")
                last_message_count = current_message_count

            # Check if task completed, failed, or timed out
            usage = _summarize_usage(getattr(result, "token_usage_records", None))
            if result.status == SubagentStatus.COMPLETED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_completed", "task_id": task_id, "result": result.result, "usage": usage})
                logger.info(f"[trace={trace_id}] Task {task_id} completed after {poll_count} polls")
                cleanup_background_task(task_id)
                return f"Task Succeeded. Result: {result.result}"
            elif result.status == SubagentStatus.FAILED:
                _report_subagent_usage(runtime, result)
                cleanup_background_task(task_id)
                stop_reason = getattr(result, "stop_reason", None)
                if retries_left > 0 and _is_retryable_failure(stop_reason, runtime_app_config):
                    attempt += 1
                    retries_left -= 1
                    retry_task_id = f"{tool_call_id}-retry{attempt}"
                    logger.warning(f"[trace={trace_id}] Task {task_id} failed, retrying as {retry_task_id} ({retries_left} retries left). Error: {result.error}")
                    writer({"type": "task_failed", "task_id": task_id, "error": result.error, "usage": usage, "retrying": True, "stop_reason": stop_reason})
                    # Reset execution state for the retry attempt.
                    executor = SubagentExecutor(**executor_kwargs)
                    task_id = executor.execute_async(_retry_prompt(prompt, result.error), task_id=retry_task_id)
                    poll_count = 0
                    last_status = None
                    last_message_count = 0
                    writer({"type": "task_started", "task_id": task_id, "description": description})
                    continue
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                writer({"type": "task_failed", "task_id": task_id, "error": result.error, "usage": usage, "stop_reason": stop_reason})
                if stop_reason:
                    logger.error(f"[trace={trace_id}] Task {task_id} failed with stop_reason={stop_reason} (not retried — a retry would hit the same ceiling): {result.error}")
                    return (
                        f"Task failed. Error: {result.error}\n\n"
                        f"Stop reason: {stop_reason} — the task ran out of its allowance rather than hitting a "
                        "transient problem, so it was NOT retried. Read the partial output first, then re-dispatch "
                        "only the part that can still make progress; a full re-run would spend the same allowance again."
                    )
                logger.error(f"[trace={trace_id}] Task {task_id} failed (no retries left): {result.error}")
                return f"Task failed. Error: {result.error}"
            elif result.status == SubagentStatus.CANCELLED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_cancelled", "task_id": task_id, "error": result.error, "usage": usage})
                logger.info(f"[trace={trace_id}] Task {task_id} cancelled: {result.error}")
                cleanup_background_task(task_id)
                return "Task cancelled by user."
            elif result.status == SubagentStatus.TIMED_OUT:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_timed_out", "task_id": task_id, "error": result.error, "usage": usage})
                logger.warning(f"[trace={trace_id}] Task {task_id} timed out: {result.error}")
                cleanup_background_task(task_id)
                # Timeouts are not retried; surface whatever the subagent finished.
                partial = _extract_partial_result(ai_messages)
                return f"Task timed out after {config.timeout_seconds}s. Error: {result.error}{partial}"

            # Still running, wait before next poll
            await asyncio.sleep(5)
            poll_count += 1

            # Polling timeout as a safety net (in case thread pool timeout doesn't work)
            # Set to execution timeout + 60s buffer, in 5s poll intervals
            # This catches edge cases where the background task gets stuck
            if poll_count > max_poll_count:
                timeout_minutes = config.timeout_seconds // 60
                logger.error(f"[trace={trace_id}] Task {task_id} polling timed out after {poll_count} polls (should have been caught by thread pool timeout)")
                _report_subagent_usage(runtime, result)
                usage = _summarize_usage(getattr(result, "token_usage_records", None))
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                writer({"type": "task_timed_out", "task_id": task_id, "usage": usage})
                # The task may still be running in the background. Signal cooperative
                # cancellation and schedule deferred cleanup to remove the entry from
                # _background_tasks once the background thread reaches a terminal state.
                request_cancel_background_task(task_id)
                _schedule_deferred_subagent_cleanup(task_id, trace_id, max_poll_count)
                return f"Task polling timed out after {timeout_minutes} minutes. This may indicate the background task is stuck. Status: {result.status.value}"
    except asyncio.CancelledError:
        # Signal the background subagent thread to stop cooperatively.
        request_cancel_background_task(task_id)

        # Wait (shielded) for the subagent to reach a terminal state so the
        # final token usage snapshot is reported to the parent RunJournal
        # before the parent worker persists get_completion_data().
        terminal_result = None
        try:
            terminal_result = await asyncio.shield(_await_subagent_terminal(task_id, max_poll_count))
        except asyncio.CancelledError:
            pass

        # Report whatever the subagent collected (even if we timed out).
        final_result = terminal_result or get_background_task_result(task_id)
        if final_result is not None:
            _report_subagent_usage(runtime, final_result)
        if final_result is not None and _is_subagent_terminal(final_result):
            cleanup_background_task(task_id)
        else:
            _schedule_deferred_subagent_cleanup(task_id, trace_id, max_poll_count)
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
    except Exception:
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
