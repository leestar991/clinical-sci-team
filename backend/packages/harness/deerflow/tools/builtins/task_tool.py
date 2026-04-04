"""Task tool for delegating work to subagents."""

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import Annotated, Literal

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langgraph.config import get_stream_writer
from langgraph.typing import ContextT

from deerflow.agents.lead_agent.prompt import get_skills_prompt_section
from deerflow.agents.thread_state import ThreadState
from deerflow.sandbox.security import LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE, is_host_bash_allowed
from deerflow.subagents import SubagentExecutor, get_available_subagent_names, get_subagent_config
from deerflow.subagents.executor import SubagentStatus, cleanup_background_task, get_background_task_result

logger = logging.getLogger(__name__)


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    description: str,
    prompt: str,
    subagent_type: Literal[
        "general-purpose",
        "bash",
        "literature-analyzer",
        "data-extractor",
        "report-writer",
        "ov-retriever",
        "cmo-gpl",
        "gpm",
        "parkinson-clinical",
        "trial-design",
        "trial-statistics",
        "data-management",
        "drug-registration",
        "pharmacology",
        "toxicology",
        "chemistry",
        "bioinformatics",
        "clinical-ops",
        "quality-control",
        "report-writing",
    ],
    tool_call_id: Annotated[str, InjectedToolCallId],
    max_turns: int | None = None,
) -> str:
    """Delegate a task to a specialized subagent that runs in its own context.

    Subagents help you:
    - Preserve context by keeping exploration and implementation separate
    - Handle complex multi-step tasks autonomously
    - Execute commands or operations in isolated contexts

    Available subagent types depend on the active sandbox configuration:
    - **general-purpose**: A capable agent for complex, multi-step tasks that require
      both exploration and action. Use when the task requires complex reasoning,
      multiple dependent steps, or would benefit from isolated context.
    - **bash**: Command execution specialist for running bash commands. This is only
      available when host bash is explicitly allowed or when using an isolated shell
      sandbox such as `AioSandboxProvider`.
    - **literature-analyzer**: Deep academic paper analysis specialist. Use for
      structured close-reading of research papers: extracting research questions,
      methodology, findings, limitations, and differentiators from a single paper.
    - **data-extractor**: Structured data extraction specialist. Use for extracting
      numerical data, performance metrics, and comparison tables from research documents
      with high accuracy.
    - **report-writer**: Academic report writing specialist. Use for drafting specific
      sections of scientific reports (literature review, methodology, data comparison)
      from provided research notes and analysis outputs.
    - **ov-retriever**: OpenViking semantic retrieval specialist. Use for searching
      the indexed research knowledge base via `ov find` and `ov read` commands.

    Virtual Clinical Development Team (use when agent is `clinical-dev-lead`):
    - **cmo-gpl**: Chief Medical Officer / Global Project Leader. Use for clinical
      development strategy, benefit-risk assessment, and cross-functional alignment.
    - **gpm**: Global Project Manager. Use for program timelines, milestones, critical
      path analysis, risk registers, and resource planning.
    - **parkinson-clinical**: Parkinson's disease clinical expert. Use for PD
      pathophysiology, disease staging, endpoint selection (MDS-UPDRS, PDQ-39),
      patient population definition, and biomarker strategy (α-synuclein, NfL, GBA/LRRK2).
    - **trial-design**: Clinical trial design specialist. Use for protocol writing,
      randomization, blinding, endpoint selection, adaptive design, and SPIRIT/ICH E6 compliance.
    - **trial-statistics**: Biostatistics specialist. Use for sample size calculations,
      Statistical Analysis Plans, multiplicity control, interim analyses, and estimands (ICH E9R1).
    - **data-management**: Clinical data management specialist. Use for CRF design,
      CDISC CDASH/SDTM/ADaM standards, EDC setup, MedDRA/WHODrug coding, and database lock.
    - **drug-registration**: Regulatory affairs specialist. Use for IND/NDA/MAA submissions,
      FDA/EMA/NMPA regulatory pathways, CTD/eCTD structure, and agency interaction planning.
    - **pharmacology**: Clinical pharmacology and PK/PD specialist. Use for PK/PD modeling,
      ADME assessment, DDI evaluation, dose selection, and special population considerations.
    - **toxicology**: Nonclinical safety specialist. Use for GLP toxicology study packages,
      NOAEL/MABEL determination, genotoxicity, and ICH S1-S11 compliance.
    - **chemistry**: CMC and pharmaceutical chemistry specialist. Use for drug substance/product
      development, analytical methods, stability programs (ICH Q1-Q14), and CTD Module 3.
    - **bioinformatics**: Bioinformatics and translational science specialist. Use for
      biomarker strategy (BEST framework), genomics (GBA/LRRK2/SNCA), companion diagnostics,
      and multi-omics analysis.
    - **clinical-ops**: Clinical operations specialist. Use for site selection, patient
      enrollment strategy, CRO management, risk-based monitoring (ICH E6 R2), and IMP supply.
    - **quality-control**: GxP quality and compliance specialist. Use for GCP/GLP/GMP
      compliance, CAPA plans, inspection readiness, TMF management, and audit programs.
    - **report-writing**: Clinical/regulatory medical writer. Use for CSR (ICH E3), IB,
      protocol synopses, regulatory briefing documents, and patient narratives.

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
        prompt: The task description for the subagent. Be specific and clear about what needs to be done. ALWAYS PROVIDE THIS PARAMETER SECOND.
        subagent_type: The type of subagent to use. ALWAYS PROVIDE THIS PARAMETER THIRD.
        max_turns: Optional maximum number of agent turns. Defaults to subagent's configured max.
    """
    available_subagent_names = get_available_subagent_names()

    # Get subagent configuration
    config = get_subagent_config(subagent_type)
    if config is None:
        available = ", ".join(available_subagent_names)
        return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"
    if subagent_type == "bash" and not is_host_bash_allowed():
        return f"Error: {LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE}"

    # Build config overrides
    overrides: dict = {}

    skills_section = get_skills_prompt_section()
    if skills_section:
        overrides["system_prompt"] = config.system_prompt + "\n\n" + skills_section

    if max_turns is not None:
        overrides["max_turns"] = max_turns

    if overrides:
        config = replace(config, **overrides)

    # Extract parent context from runtime
    sandbox_state = None
    thread_data = None
    thread_id = None
    parent_model = None
    trace_id = None

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

    # Get available tools (excluding task tool to prevent nesting)
    # Lazy import to avoid circular dependency
    from deerflow.tools import get_available_tools

    # Subagents should not have subagent tools enabled (prevent recursive nesting)
    tools = get_available_tools(model_name=parent_model, subagent_enabled=False)

    # Create executor
    executor = SubagentExecutor(
        config=config,
        tools=tools,
        parent_model=parent_model,
        sandbox_state=sandbox_state,
        thread_data=thread_data,
        thread_id=thread_id,
        trace_id=trace_id,
    )

    # Start background execution (always async to prevent blocking)
    # Use tool_call_id as task_id for better traceability
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # Poll for task completion in backend (removes need for LLM to poll)
    poll_count = 0
    last_status = None
    last_message_count = 0  # Track how many AI messages we've already sent
    # Polling timeout: execution timeout + 60s buffer, checked every 5s
    max_poll_count = (config.timeout_seconds + 60) // 5

    logger.info(f"[trace={trace_id}] Started background task {task_id} (subagent={subagent_type}, timeout={config.timeout_seconds}s, polling_limit={max_poll_count} polls)")

    writer = get_stream_writer()
    # Send Task Started message'
    writer({"type": "task_started", "task_id": task_id, "description": description})
    writer({"type": "task_todo_sync", "action": "in_progress", "description": description, "task_id": task_id})

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
            current_message_count = len(result.ai_messages)
            if current_message_count > last_message_count:
                # Send task_running event for each new message
                for i in range(last_message_count, current_message_count):
                    message = result.ai_messages[i]
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
            if result.status == SubagentStatus.COMPLETED:
                writer({"type": "task_completed", "task_id": task_id, "result": result.result})
                writer({"type": "task_todo_sync", "action": "completed", "description": description, "task_id": task_id, "summary": (result.result or "")[:200]})
                logger.info(f"[trace={trace_id}] Task {task_id} completed after {poll_count} polls")
                cleanup_background_task(task_id)
                return f"Task Succeeded. Result: {result.result}"
            elif result.status == SubagentStatus.FAILED:
                writer({"type": "task_failed", "task_id": task_id, "error": result.error})
                writer({"type": "task_todo_sync", "action": "failed", "description": description, "task_id": task_id})
                logger.error(f"[trace={trace_id}] Task {task_id} failed: {result.error}")
                cleanup_background_task(task_id)
                return f"Task failed. Error: {result.error}"
            elif result.status == SubagentStatus.TIMED_OUT:
                writer({"type": "task_timed_out", "task_id": task_id, "error": result.error})
                writer({"type": "task_todo_sync", "action": "failed", "description": description, "task_id": task_id})
                logger.warning(f"[trace={trace_id}] Task {task_id} timed out: {result.error}")
                cleanup_background_task(task_id)
                return f"Task timed out. Error: {result.error}"

            # Still running, wait before next poll
            await asyncio.sleep(5)
            poll_count += 1

            # Polling timeout as a safety net (in case thread pool timeout doesn't work)
            # Set to execution timeout + 60s buffer, in 5s poll intervals
            # This catches edge cases where the background task gets stuck
            # Note: We don't call cleanup_background_task here because the task may
            # still be running in the background. The cleanup will happen when the
            # executor completes and sets a terminal status.
            if poll_count > max_poll_count:
                timeout_minutes = config.timeout_seconds // 60
                logger.error(f"[trace={trace_id}] Task {task_id} polling timed out after {poll_count} polls (should have been caught by thread pool timeout)")
                writer({"type": "task_timed_out", "task_id": task_id})
                writer({"type": "task_todo_sync", "action": "failed", "description": description, "task_id": task_id})
                return f"Task polling timed out after {timeout_minutes} minutes. This may indicate the background task is stuck. Status: {result.status.value}"
    except asyncio.CancelledError:

        async def cleanup_when_done() -> None:
            max_cleanup_polls = max_poll_count
            cleanup_poll_count = 0

            while True:
                result = get_background_task_result(task_id)
                if result is None:
                    return

                if result.status in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.TIMED_OUT} or getattr(result, "completed_at", None) is not None:
                    cleanup_background_task(task_id)
                    return

                if cleanup_poll_count > max_cleanup_polls:
                    logger.warning(f"[trace={trace_id}] Deferred cleanup for task {task_id} timed out after {cleanup_poll_count} polls")
                    return

                await asyncio.sleep(5)
                cleanup_poll_count += 1

        def log_cleanup_failure(cleanup_task: asyncio.Task[None]) -> None:
            if cleanup_task.cancelled():
                return

            exc = cleanup_task.exception()
            if exc is not None:
                logger.error(f"[trace={trace_id}] Deferred cleanup failed for task {task_id}: {exc}")

        logger.debug(f"[trace={trace_id}] Scheduling deferred cleanup for cancelled task {task_id}")
        asyncio.create_task(cleanup_when_done()).add_done_callback(log_cleanup_failure)
        raise
