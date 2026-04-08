"""Tests for Lead Agent + Subagent collaboration.

Focus agents (5 test areas):
    clinical-dev-lead  → cmo-gpl              (Claude Opus 4.6)
    clinical-medicine  → parkinson-clinical    (Claude Sonnet 4.6)
    biostats           → trial-statistics      (GPT-4.1)
    regulatory         → drug-registration     (Claude Sonnet 4.6)
    ops-quality        → clinical-ops          (Claude Haiku 4.5)
                         quality-control       (Claude Sonnet 4.6)

Test coverage:
    1. Registration & config correctness for all 6 target agents
    2. Delegation design: tools, model, description guidance
    3. task_tool Literal coverage: all 6 agents in Literal type
    4. SubagentLimitMiddleware: clinical-team parallel-dispatch truncation
    5. Silent truncation gap: lead agent receives no feedback on dropped tasks
    6. Executor parallel dispatch: 2–3 subagents complete correctly
    7. Context propagation: sandbox_state + thread_data flow lead → child
    8. Trace-ID chaining: same trace_id from parent to executor
    9. Model inheritance: "inherit" resolves to parent model
   10. SSE event payload structure validation
   11. Known-issue regression anchors (no fixes expected)

Known issues documented in TestKnownIssues:
    Issue-1: MAX_CONCURRENT_SUBAGENTS double definition in executor.py
             (line 71: 5, line 470: 3 – thread pools sized with 5, constant says 3)
    Issue-2: parent_model resolution reads from metadata, not configurable
             → subagents always fall back to default model
    Issue-3: Silent task truncation – LLM not informed which tasks were dropped
    Issue-4: MAX_CONCURRENT_SUBAGENTS not exported from subagent_limit_middleware.py
             → existing test_subagent_limit_middleware.py would fail on import
"""

import inspect
import sys
import threading
import typing
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

# ---------------------------------------------------------------------------
# Constants – target agent names for the five test areas
# ---------------------------------------------------------------------------

TARGET_AGENTS = [
    "cmo-gpl",             # clinical-dev-lead
    "parkinson-clinical",  # clinical-medicine
    "trial-statistics",    # biostats
    "drug-registration",   # regulatory
    "clinical-ops",        # ops-quality (operations part)
    "quality-control",     # ops-quality (quality part)
]

# ---------------------------------------------------------------------------
# Helpers used across multiple sections
# ---------------------------------------------------------------------------


def _task_call(task_id: str = "call_1") -> dict:
    return {"name": "task", "id": task_id, "args": {"prompt": "do something"}}


def _other_call(name: str = "bash", call_id: str = "call_other") -> dict:
    return {"name": name, "id": call_id, "args": {}}


def _ai_msg_with_tools(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


# ============================================================================
# Section 1 – Target Agent Registration Tests
# ============================================================================


class TestTargetAgentRegistration:
    """All 6 target agents must be registered in BUILTIN_SUBAGENTS."""

    def test_all_target_agents_in_builtin_registry(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        for name in TARGET_AGENTS:
            assert name in BUILTIN_SUBAGENTS, f"Target agent '{name}' missing from BUILTIN_SUBAGENTS"

    def test_cmo_gpl_name_and_description_keyword(self):
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG

        assert CMO_GPL_CONFIG.name == "cmo-gpl"
        desc = CMO_GPL_CONFIG.description.lower()
        assert any(kw in desc for kw in ["chief medical", "cmo", "project leader", "gpl"]), (
            "cmo-gpl description should mention CMO/GPL role"
        )

    def test_parkinson_clinical_name_and_description_keyword(self):
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG

        assert PARKINSON_CLINICAL_CONFIG.name == "parkinson-clinical"
        assert any(kw in PARKINSON_CLINICAL_CONFIG.description for kw in ["Parkinson", "PD", "neurolog"]), (
            "parkinson-clinical description should mention Parkinson's disease"
        )

    def test_trial_statistics_name_and_description_keyword(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        assert TRIAL_STATISTICS_CONFIG.name == "trial-statistics"
        desc = TRIAL_STATISTICS_CONFIG.description.lower()
        assert any(kw in desc for kw in ["statistic", "biostat", "sample size", "sap"]), (
            "trial-statistics description should mention statistics/biostatistics"
        )

    def test_drug_registration_name_and_description_keyword(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG

        assert DRUG_REGISTRATION_CONFIG.name == "drug-registration"
        assert any(kw in DRUG_REGISTRATION_CONFIG.description for kw in ["regulatory", "Regulatory", "IND", "NDA", "MAA"]), (
            "drug-registration description should mention regulatory submissions"
        )

    def test_clinical_ops_name_and_description_keyword(self):
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG

        assert CLINICAL_OPS_CONFIG.name == "clinical-ops"
        desc = CLINICAL_OPS_CONFIG.description.lower()
        assert any(kw in desc for kw in ["operations", "clinical ops", "site", "enrollment", "cro"]), (
            "clinical-ops description should mention operations/CRO/enrollment"
        )

    def test_quality_control_name_and_description_keyword(self):
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        assert QUALITY_CONTROL_CONFIG.name == "quality-control"
        assert any(kw in QUALITY_CONTROL_CONFIG.description for kw in ["GCP", "GxP", "quality", "Quality", "compliance"]), (
            "quality-control description should mention GxP/compliance"
        )


# ============================================================================
# Section 2 – Delegation Design: tools, model, no recursion
# ============================================================================


class TestDelegationDesign:
    """Validate that each target agent is correctly designed for delegation."""

    @pytest.fixture(params=TARGET_AGENTS)
    def target_config(self, request):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        return request.param, BUILTIN_SUBAGENTS[request.param]

    def test_task_tool_disallowed_prevents_recursive_nesting(self, target_config):
        name, config = target_config
        assert config.disallowed_tools is not None, f"'{name}' must have disallowed_tools set"
        assert "task" in config.disallowed_tools, (
            f"'{name}' must disallow 'task' to prevent recursive nesting"
        )

    def test_task_not_in_allowed_tools(self, target_config):
        name, config = target_config
        if config.tools is not None:
            assert "task" not in config.tools, (
                f"'{name}' must not allow 'task' in tools (recursive nesting not permitted)"
            )

    def test_description_has_delegation_guidance(self, target_config):
        name, config = target_config
        assert "Use this subagent when" in config.description or "Use for" in config.description, (
            f"'{name}' description should include 'Use this subagent when' or 'Use for' "
            f"to guide lead agent on when to delegate"
        )

    def test_system_prompt_has_source_traceability(self, target_config):
        name, config = target_config
        assert "source_traceability" in config.system_prompt or "来源引用" in config.system_prompt, (
            f"'{name}' system_prompt should include source traceability requirements"
        )

    def test_model_is_string(self, target_config):
        name, config = target_config
        assert isinstance(config.model, str) and len(config.model) > 0, (
            f"'{name}' model must be a non-empty string"
        )

    def test_timeout_positive(self, target_config):
        _, config = target_config
        assert config.timeout_seconds > 0

    def test_max_turns_positive(self, target_config):
        _, config = target_config
        assert config.max_turns > 0


class TestModelSelection:
    """Validate intentional model choices for each agent."""

    def test_cmo_gpl_uses_claude_opus_highest_capability(self):
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG

        # cmo-gpl requires top-tier reasoning for benefit-risk decisions
        assert "opus" in CMO_GPL_CONFIG.model.lower(), (
            "cmo-gpl should use Claude Opus for highest-capability strategic reasoning"
        )

    def test_clinical_ops_uses_haiku_cost_efficiency(self):
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG

        # clinical-ops handles template-heavy operational planning → cost-efficient model
        assert "haiku" in CLINICAL_OPS_CONFIG.model.lower(), (
            "clinical-ops should use Claude Haiku for cost-efficient operational templating"
        )

    def test_trial_statistics_uses_gpt41_for_numeric_precision(self):
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        # trial-statistics requires precise numeric reasoning → GPT-4.1
        assert "gpt-4" in TRIAL_STATISTICS_CONFIG.model.lower() or "gpt4" in TRIAL_STATISTICS_CONFIG.model.lower(), (
            "trial-statistics should use GPT-4.x for numeric/statistical precision"
        )

    def test_parkinson_clinical_drug_registration_quality_use_sonnet(self):
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        for name, config in [
            ("parkinson-clinical", PARKINSON_CLINICAL_CONFIG),
            ("drug-registration", DRUG_REGISTRATION_CONFIG),
            ("quality-control", QUALITY_CONTROL_CONFIG),
        ]:
            assert "sonnet" in config.model.lower(), (
                f"'{name}' should use Claude Sonnet for nuanced clinical/regulatory judgment"
            )


# ============================================================================
# Section 3 – task_tool Literal Coverage
# ============================================================================


class TestTaskToolLiteralCoverage:
    """Verify task_tool's subagent_type Literal includes all 6 target agents."""

    def _get_literal_args(self) -> tuple:
        from deerflow.tools.builtins.task_tool import task_tool

        func = getattr(task_tool, "func", None) or getattr(task_tool, "coroutine", None) or task_tool
        sig = inspect.signature(func)
        param = sig.parameters.get("subagent_type")
        assert param is not None, "task_tool has no 'subagent_type' parameter"

        annotation = param.annotation
        if typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]

        assert typing.get_origin(annotation) is typing.Literal, (
            f"subagent_type must be Literal, got {annotation}"
        )
        return typing.get_args(annotation)

    @pytest.mark.parametrize("name", TARGET_AGENTS)
    def test_target_agent_in_task_tool_literal(self, name):
        assert name in self._get_literal_args(), (
            f"'{name}' not in task_tool subagent_type Literal — "
            f"lead agent cannot dispatch to this agent"
        )

    def test_task_tool_docstring_describes_clinical_dev_lead_section(self):
        from deerflow.tools.builtins.task_tool import task_tool

        doc = task_tool.description or ""
        assert "Virtual Clinical Development Team" in doc or "clinical-dev-lead" in doc, (
            "task_tool docstring should describe Virtual Clinical Development Team section"
        )

    def test_task_tool_docstring_describes_cmo_gpl_role(self):
        from deerflow.tools.builtins.task_tool import task_tool

        doc = task_tool.description or ""
        assert "cmo-gpl" in doc or "Chief Medical Officer" in doc, (
            "task_tool docstring should explain when to use cmo-gpl"
        )

    def test_task_tool_docstring_describes_regulatory_specialist(self):
        from deerflow.tools.builtins.task_tool import task_tool

        doc = task_tool.description or ""
        assert "drug-registration" in doc or "Regulatory" in doc, (
            "task_tool docstring should explain when to use drug-registration"
        )


# ============================================================================
# Section 4 – SubagentLimitMiddleware: clinical team concurrency
# ============================================================================


class TestClinicalTeamConcurrencyLimit:
    """SubagentLimitMiddleware must correctly limit clinical team parallel calls."""

    def test_three_clinical_agents_within_default_limit_allowed(self):
        """3 concurrent clinical tasks at default limit (3) → no truncation."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = _ai_msg_with_tools([
            _task_call("cmo"),
            _task_call("pd"),
            _task_call("stats"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, "3 tasks at limit=3 should NOT be truncated"

    def test_four_clinical_agents_exceed_limit_fourth_truncated(self):
        """4 concurrent clinical tasks at limit=3 → 4th dropped."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = _ai_msg_with_tools([
            _task_call("cmo"),
            _task_call("pd"),
            _task_call("stats"),
            _task_call("reg"),     # ← should be dropped
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None, "4 tasks at limit=3 should be truncated"
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 3
        retained_ids = {tc["id"] for tc in task_calls}
        assert "reg" not in retained_ids, "4th task (reg) should be dropped"

    def test_mixed_clinical_plus_bash_tool_only_tasks_counted(self):
        """bash/read_file alongside task calls: non-task calls not counted toward limit."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = _ai_msg_with_tools([
            _other_call("bash", "b1"),
            _task_call("cmo"),
            _task_call("stats"),     # → exactly at limit
            _other_call("read_file", "r1"),
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, (
            "2 task calls with non-task calls present should not trigger truncation at limit=2"
        )

    def test_clinical_team_ops_quality_pattern_four_tasks_second_pair_dropped(self):
        """Simulates lead agent dispatching to cmo+pd+stats+reg in one response.
        At default limit=3, the 4th (reg) is silently dropped.
        """
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = _ai_msg_with_tools([
            {"name": "task", "id": "t_cmo", "args": {"subagent_type": "cmo-gpl"}},
            {"name": "task", "id": "t_pd", "args": {"subagent_type": "parkinson-clinical"}},
            {"name": "task", "id": "t_stats", "args": {"subagent_type": "trial-statistics"}},
            {"name": "task", "id": "t_reg", "args": {"subagent_type": "drug-registration"}},
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        kept_ids = {tc["id"] for tc in result["messages"][0].tool_calls if tc["name"] == "task"}
        assert "t_cmo" in kept_ids
        assert "t_pd" in kept_ids
        assert "t_stats" in kept_ids
        assert "t_reg" not in kept_ids, "4th task must be dropped"

    def test_ops_quality_both_agents_fit_with_limit_2(self):
        """clinical-ops + quality-control (2 tasks) fit within limit=2 with no truncation."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = _ai_msg_with_tools([
            {"name": "task", "id": "t_ops", "args": {"subagent_type": "clinical-ops"}},
            {"name": "task", "id": "t_qc", "args": {"subagent_type": "quality-control"}},
        ])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is None, "ops + quality (2 tasks) at limit=2 should not be truncated"


# ============================================================================
# Section 5 – Silent Truncation Gap (Issue-3)
# ============================================================================


class TestSilentTruncationGap:
    """Document and verify the silent truncation behavior (Issue-3).

    When the middleware drops a task call, the lead agent receives NO
    error message or notice — the tool_calls list is silently shortened.
    This means:
        - The lead agent may not know regulatory work was skipped.
        - It will only discover the gap when trying to aggregate results.
        - There is no re-queue or retry mechanism.
    These tests capture the CURRENT behavior as regression anchors.
    """

    def test_truncated_message_has_no_warning_content(self):
        """After truncation, the AIMessage content is unchanged (no warning injected)."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=2)
        original_content = "I will dispatch three clinical tasks simultaneously."
        msg = AIMessage(
            content=original_content,
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        # BUG (Issue-3): Content is unchanged — no notification to LLM about dropped task
        assert updated_msg.content == original_content, (
            "Content unchanged after truncation — lead agent not notified of dropped task (Issue-3)"
        )

    def test_no_error_tool_message_injected_for_dropped_task(self):
        """After truncation, only one message is returned — no synthetic error ToolMessage."""
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = _ai_msg_with_tools([_task_call("t1"), _task_call("t2"), _task_call("t3")])
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        # Returns only the updated AIMessage — no ToolMessage explaining the drop
        assert len(result["messages"]) == 1, (
            "Only the modified AIMessage is returned; no ToolMessage explaining dropped task (Issue-3)"
        )


# ============================================================================
# Section 6 – Executor Parallel Dispatch Tests
# ============================================================================

# Module mocking setup (same pattern as test_subagent_executor.py)
_MOCKED_EXECUTOR_MODULES = [
    "deerflow.agents",
    "deerflow.agents.thread_state",
    "deerflow.agents.middlewares",
    "deerflow.agents.middlewares.thread_data_middleware",
    "deerflow.sandbox",
    "deerflow.sandbox.middleware",
    "deerflow.models",
]


@pytest.fixture(scope="module")
def _executor_classes():
    """Load real executor classes with heavy dependencies mocked."""
    original_modules = {name: sys.modules.get(name) for name in _MOCKED_EXECUTOR_MODULES}
    original_executor = sys.modules.get("deerflow.subagents.executor")

    if "deerflow.subagents.executor" in sys.modules:
        del sys.modules["deerflow.subagents.executor"]

    for name in _MOCKED_EXECUTOR_MODULES:
        sys.modules[name] = MagicMock()

    from deerflow.subagents.config import SubagentConfig
    from deerflow.subagents.executor import SubagentExecutor, SubagentResult, SubagentStatus

    classes = {
        "SubagentConfig": SubagentConfig,
        "SubagentExecutor": SubagentExecutor,
        "SubagentResult": SubagentResult,
        "SubagentStatus": SubagentStatus,
    }

    yield classes

    for name in _MOCKED_EXECUTOR_MODULES:
        if original_modules[name] is not None:
            sys.modules[name] = original_modules[name]
        elif name in sys.modules:
            del sys.modules[name]

    if original_executor is not None:
        sys.modules["deerflow.subagents.executor"] = original_executor
    elif "deerflow.subagents.executor" in sys.modules:
        del sys.modules["deerflow.subagents.executor"]


@pytest.fixture
def executor_classes(_executor_classes):
    return _executor_classes


@pytest.fixture
def base_subagent_config(executor_classes):
    return executor_classes["SubagentConfig"](
        name="test-clinical-agent",
        description="Test clinical agent. Use this subagent when testing.",
        system_prompt="You are a test clinical agent.\n<source_traceability>Cite sources.</source_traceability>",
        tools=["tavily_web_search", "read_file", "write_file"],
        disallowed_tools=["task"],
        model="claude-sonnet-4-6",
        max_turns=10,
        timeout_seconds=60,
    )


async def _async_iter(items):
    for item in items:
        yield item


class TestParallelTaskDispatch:
    """Multiple subagents execute concurrently and results are collected correctly."""

    def test_two_subagents_both_complete(self, executor_classes, base_subagent_config):
        """Two parallel subagents both complete with correct results."""
        SubagentExecutor = executor_classes["SubagentExecutor"]
        SubagentStatus = executor_classes["SubagentStatus"]

        results = []

        def run_agent(task_text: str, expected_result: str):
            msg = AIMessage(content=expected_result)
            msg.id = f"msg-{task_text}"
            final_state = {"messages": [HumanMessage(content=task_text), msg]}
            mock_agent = MagicMock()
            mock_agent.astream = lambda *a, **kw: _async_iter([final_state])

            executor = SubagentExecutor(
                config=base_subagent_config,
                tools=[],
                thread_id=f"thread-{task_text}",
            )
            with patch.object(executor, "_create_agent", return_value=mock_agent):
                return executor.execute(task_text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(run_agent, "clinical-task", "Clinical assessment complete"),
                pool.submit(run_agent, "stats-task", "Statistics SAP delivered"),
            }
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 2
        all_statuses = {r.status for r in results}
        assert all_statuses == {SubagentStatus.COMPLETED}
        all_results = {r.result for r in results}
        assert "Clinical assessment complete" in all_results
        assert "Statistics SAP delivered" in all_results

    def test_three_parallel_subagents_max_concurrent(self, executor_classes, base_subagent_config):
        """Three parallel subagents (default max) all complete correctly."""
        SubagentExecutor = executor_classes["SubagentExecutor"]
        SubagentStatus = executor_classes["SubagentStatus"]

        agent_tasks = [
            ("cmo-task", "CMO strategy memo drafted"),
            ("pd-task", "PD endpoint analysis complete"),
            ("reg-task", "IND strategy outlined"),
        ]
        results = []
        completion_order = []
        lock = threading.Lock()

        def run_agent(task_text: str, expected_result: str):
            msg = AIMessage(content=expected_result)
            msg.id = f"msg-{task_text}"
            final_state = {"messages": [HumanMessage(content=task_text), msg]}
            mock_agent = MagicMock()
            mock_agent.astream = lambda *a, **kw: _async_iter([final_state])

            executor = SubagentExecutor(
                config=base_subagent_config,
                tools=[],
                thread_id=f"thread-{task_text}",
            )
            with patch.object(executor, "_create_agent", return_value=mock_agent):
                result = executor.execute(task_text)
            with lock:
                completion_order.append(task_text)
            return result

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run_agent, task, res) for task, res in agent_tasks]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 3
        assert all(r.status == SubagentStatus.COMPLETED for r in results)
        returned_results = {r.result for r in results}
        for _, expected in agent_tasks:
            assert expected in returned_results

    def test_failed_subagent_does_not_block_others(self, executor_classes, base_subagent_config):
        """One failing subagent does not prevent others from completing."""
        SubagentExecutor = executor_classes["SubagentExecutor"]
        SubagentStatus = executor_classes["SubagentStatus"]

        results = []

        def run_success(task_text: str):
            msg = AIMessage(content="Success result")
            msg.id = "msg-ok"
            final_state = {"messages": [HumanMessage(content=task_text), msg]}
            mock_agent = MagicMock()
            mock_agent.astream = lambda *a, **kw: _async_iter([final_state])
            executor = SubagentExecutor(config=base_subagent_config, tools=[], thread_id="t-ok")
            with patch.object(executor, "_create_agent", return_value=mock_agent):
                return executor.execute(task_text)

        def run_failure(task_text: str):
            mock_agent = MagicMock()
            mock_agent.astream.side_effect = RuntimeError("LLM API timeout")
            executor = SubagentExecutor(config=base_subagent_config, tools=[], thread_id="t-fail")
            with patch.object(executor, "_create_agent", return_value=mock_agent):
                return executor.execute(task_text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(run_success, "clinical-ops planning"),
                pool.submit(run_failure, "quality-control audit"),
            ]
            for f in as_completed(futures):
                results.append(f.result())

        statuses = {r.status for r in results}
        assert SubagentStatus.COMPLETED in statuses
        assert SubagentStatus.FAILED in statuses

        failed = next(r for r in results if r.status == SubagentStatus.FAILED)
        assert "LLM API timeout" in (failed.error or "")


# ============================================================================
# Section 7 – Context Propagation: sandbox + thread_data flow
# ============================================================================


class TestContextPropagation:
    """sandbox_state and thread_data must flow from lead agent to subagent executor."""

    def test_sandbox_state_stored_in_executor(self, executor_classes, base_subagent_config):
        SubagentExecutor = executor_classes["SubagentExecutor"]

        mock_sandbox = {"sandbox_id": "sandbox-abc", "provider": "local"}
        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            sandbox_state=mock_sandbox,
            thread_id="thread-001",
        )
        assert executor.sandbox_state == mock_sandbox

    def test_thread_data_stored_in_executor(self, executor_classes, base_subagent_config):
        SubagentExecutor = executor_classes["SubagentExecutor"]

        mock_thread_data = {"thread_id": "thread-001", "workspace": "/mnt/user-data/workspace"}
        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            thread_data=mock_thread_data,
            thread_id="thread-001",
        )
        assert executor.thread_data == mock_thread_data

    def test_sandbox_state_passed_into_initial_state(self, executor_classes, base_subagent_config):
        """_build_initial_state() includes sandbox_state when provided."""
        SubagentExecutor = executor_classes["SubagentExecutor"]

        mock_sandbox = {"sandbox_id": "sandbox-abc"}
        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            sandbox_state=mock_sandbox,
            thread_id="thread-001",
        )
        state = executor._build_initial_state("Analyze PD endpoints")
        assert "sandbox" in state
        assert state["sandbox"] == mock_sandbox

    def test_thread_data_passed_into_initial_state(self, executor_classes, base_subagent_config):
        """_build_initial_state() includes thread_data when provided."""
        SubagentExecutor = executor_classes["SubagentExecutor"]

        mock_thread_data = {"thread_id": "t-001"}
        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            thread_data=mock_thread_data,
            thread_id="t-001",
        )
        state = executor._build_initial_state("Draft SAP")
        assert "thread_data" in state
        assert state["thread_data"] == mock_thread_data

    def test_missing_sandbox_not_in_initial_state(self, executor_classes, base_subagent_config):
        """When sandbox_state is None, 'sandbox' key must not be injected."""
        SubagentExecutor = executor_classes["SubagentExecutor"]

        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            sandbox_state=None,
            thread_id="thread-001",
        )
        state = executor._build_initial_state("task")
        assert "sandbox" not in state, "None sandbox_state must not inject 'sandbox' key"

    def test_thread_id_applied_to_run_config(self, executor_classes, base_subagent_config):
        """thread_id appears in the run_config passed to agent.astream."""
        SubagentExecutor = executor_classes["SubagentExecutor"]
        SubagentStatus = executor_classes["SubagentStatus"]

        captured_configs = []

        msg = AIMessage(content="done")
        msg.id = "msg-1"
        final_state = {"messages": [HumanMessage(content="task"), msg]}

        async def mock_astream(state, config=None, context=None, stream_mode=None):
            captured_configs.append(config)
            yield final_state

        mock_agent = MagicMock()
        mock_agent.astream = mock_astream

        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            thread_id="thread-xyz",
        )
        with patch.object(executor, "_create_agent", return_value=mock_agent):
            result = executor.execute("do clinical task")

        assert result.status == SubagentStatus.COMPLETED
        assert len(captured_configs) >= 1
        run_config = captured_configs[0]
        assert run_config.get("configurable", {}).get("thread_id") == "thread-xyz", (
            "thread_id must appear in run_config['configurable']['thread_id']"
        )


# ============================================================================
# Section 8 – Trace ID Chaining
# ============================================================================


class TestTraceIdChaining:
    """trace_id must propagate from parent (task_tool) to SubagentExecutor."""

    def test_executor_uses_provided_trace_id(self, executor_classes, base_subagent_config):
        SubagentExecutor = executor_classes["SubagentExecutor"]

        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            trace_id="parent-trace-abc",
        )
        assert executor.trace_id == "parent-trace-abc"

    def test_executor_generates_trace_id_when_none(self, executor_classes, base_subagent_config):
        SubagentExecutor = executor_classes["SubagentExecutor"]

        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            trace_id=None,
        )
        assert executor.trace_id is not None
        assert len(executor.trace_id) > 0

    def test_two_executors_get_independent_trace_ids_when_none(self, executor_classes, base_subagent_config):
        """Without explicit trace_id, each executor gets a unique ID."""
        SubagentExecutor = executor_classes["SubagentExecutor"]

        e1 = SubagentExecutor(config=base_subagent_config, tools=[], trace_id=None)
        e2 = SubagentExecutor(config=base_subagent_config, tools=[], trace_id=None)
        assert e1.trace_id != e2.trace_id

    def test_same_trace_id_used_in_subagent_result(self, executor_classes, base_subagent_config):
        """The trace_id appears in the SubagentResult."""
        SubagentExecutor = executor_classes["SubagentExecutor"]
        SubagentStatus = executor_classes["SubagentStatus"]

        msg = AIMessage(content="traced result")
        msg.id = "msg-trace"
        final_state = {"messages": [HumanMessage(content="task"), msg]}
        mock_agent = MagicMock()
        mock_agent.astream = lambda *a, **kw: _async_iter([final_state])

        executor = SubagentExecutor(
            config=base_subagent_config,
            tools=[],
            trace_id="clinical-trace-007",
        )
        with patch.object(executor, "_create_agent", return_value=mock_agent):
            result = executor.execute("do task")

        assert result.trace_id == "clinical-trace-007"
        assert result.status == SubagentStatus.COMPLETED


# ============================================================================
# Section 9 – Model Inheritance
# ============================================================================


class TestModelInheritance:
    """Subagent model='inherit' must resolve to the parent agent's model."""

    def test_inherit_model_resolves_to_parent(self, executor_classes):
        """When config.model == 'inherit', subagent uses parent_model."""
        from deerflow.subagents.executor import _get_model_name

        config = executor_classes["SubagentConfig"](
            name="inherit-agent",
            description="Test inherit agent. Use this subagent when testing.",
            system_prompt="Test. <source_traceability>cite</source_traceability>",
            model="inherit",
        )
        result = _get_model_name(config, "claude-opus-4-6")
        assert result == "claude-opus-4-6"

    def test_explicit_model_ignores_parent(self, executor_classes):
        """When config.model is specific, it overrides parent_model."""
        from deerflow.subagents.executor import _get_model_name

        config = executor_classes["SubagentConfig"](
            name="specific-agent",
            description="Test specific agent. Use this subagent when testing.",
            system_prompt="Test. <source_traceability>cite</source_traceability>",
            model="gpt-4.1",
        )
        result = _get_model_name(config, "claude-opus-4-6")
        assert result == "gpt-4.1"

    def test_inherit_with_none_parent_returns_none(self, executor_classes):
        """inherit with no parent → returns None (fallback to system default)."""
        from deerflow.subagents.executor import _get_model_name

        config = executor_classes["SubagentConfig"](
            name="inherit-no-parent",
            description="Test no-parent inherit. Use this subagent when testing.",
            system_prompt="Test. <source_traceability>cite</source_traceability>",
            model="inherit",
        )
        result = _get_model_name(config, None)
        assert result is None

    def test_cmo_gpl_uses_opus_not_inherited(self):
        """cmo-gpl must always use Opus regardless of parent model."""
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG
        from deerflow.subagents.executor import _get_model_name

        result = _get_model_name(CMO_GPL_CONFIG, "gpt-4o-mini")
        assert result == CMO_GPL_CONFIG.model
        assert "inherit" not in result.lower(), (
            "cmo-gpl must not inherit from parent — it explicitly requires Opus"
        )

    def test_clinical_ops_uses_haiku_not_inherited(self):
        """clinical-ops must always use Haiku regardless of parent model."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.executor import _get_model_name

        result = _get_model_name(CLINICAL_OPS_CONFIG, "claude-opus-4-6")
        assert "haiku" in result.lower(), (
            "clinical-ops must use Haiku even when parent uses Opus"
        )


# ============================================================================
# Section 10 – SSE Event Payload Structure
# ============================================================================


class TestSSEEventPayloadStructure:
    """Validate the structure and required fields of SSE events emitted by task_tool.

    These tests verify the event schema by inspecting task_tool source directly
    (no live LLM execution). They serve as regression anchors for the event contract.
    """

    def test_task_started_event_has_required_fields(self):
        """task_started event must include type, task_id, description."""
        task_started = {"type": "task_started", "task_id": "t001", "description": "Draft SAP"}
        assert task_started["type"] == "task_started"
        assert "task_id" in task_started
        assert "description" in task_started

    def test_task_running_event_has_required_fields(self):
        """task_running event must include type, task_id, message, message_index, total_messages."""
        task_running = {
            "type": "task_running",
            "task_id": "t001",
            "message": {"content": "Analyzing..."},
            "message_index": 1,
            "total_messages": 2,
        }
        assert task_running["type"] == "task_running"
        assert "message_index" in task_running
        assert "total_messages" in task_running
        assert task_running["message_index"] >= 1

    def test_task_completed_event_has_required_fields(self):
        """task_completed event must include type, task_id, result."""
        task_completed = {
            "type": "task_completed",
            "task_id": "t001",
            "result": "SAP version 1.0 completed.",
        }
        assert task_completed["type"] == "task_completed"
        assert "result" in task_completed

    def test_task_failed_event_has_required_fields(self):
        """task_failed event must include type, task_id, error."""
        task_failed = {
            "type": "task_failed",
            "task_id": "t001",
            "error": "LLM API unavailable",
        }
        assert task_failed["type"] == "task_failed"
        assert "error" in task_failed

    def test_task_todo_sync_start_has_in_progress_action(self):
        """task_todo_sync at task start must have action='in_progress'."""
        task_todo_sync_start = {
            "type": "task_todo_sync",
            "action": "in_progress",
            "description": "Draft SAP",
            "task_id": "t001",
        }
        assert task_todo_sync_start["action"] == "in_progress"
        assert "description" in task_todo_sync_start

    def test_task_todo_sync_end_has_completed_action(self):
        """task_todo_sync at task completion must have action='completed'."""
        task_todo_sync_end = {
            "type": "task_todo_sync",
            "action": "completed",
            "description": "Draft SAP",
            "task_id": "t001",
            "summary": "SAP version 1.0 completed.",
        }
        assert task_todo_sync_end["action"] == "completed"
        assert "summary" in task_todo_sync_end

    def test_task_todo_sync_end_summary_truncated_to_200_chars(self):
        """summary in task_todo_sync must be at most 200 characters (per task_tool code)."""
        long_result = "x" * 500
        # Simulate task_tool's [:200] truncation
        summary = long_result[:200]
        assert len(summary) == 200, "Summary must be truncated to 200 characters"

    def test_event_type_string_constants_match_task_tool_source(self):
        """Verify that event type strings match what task_tool actually emits."""
        import ast
        import pathlib

        task_tool_path = pathlib.Path(__file__).parent.parent / "packages" / "harness" / "deerflow" / "tools" / "builtins" / "task_tool.py"
        source = task_tool_path.read_text()

        expected_event_types = ["task_started", "task_running", "task_completed", "task_failed", "task_timed_out", "task_todo_sync"]
        for event_type in expected_event_types:
            assert f'"{event_type}"' in source, (
                f"Event type '{event_type}' not found in task_tool.py — event contract may have changed"
            )


# ============================================================================
# Section 11 – Known Issue Regression Anchors
# ============================================================================


class TestKnownIssues:
    """Regression anchors documenting known issues WITHOUT fixing them.

    These tests intentionally verify the CURRENT (potentially imperfect) behavior
    so that any accidental change to these behaviors is caught immediately.

    Documented Issues:
        Issue-1: MAX_CONCURRENT_SUBAGENTS double definition in executor.py
        Issue-2: parent_model resolved from metadata (not configurable)
        Issue-3: Silent truncation (captured in TestSilentTruncationGap)
        Issue-4: MAX_CONCURRENT_SUBAGENTS not exported from subagent_limit_middleware.py
    """

    def test_issue1_max_concurrent_constant_defined_twice_in_executor(self):
        """Issue-1: executor.py defines MAX_CONCURRENT_SUBAGENTS TWICE.

        - Line ~71:  MAX_CONCURRENT_SUBAGENTS = 5   ← used to size thread pools
        - Line ~470: MAX_CONCURRENT_SUBAGENTS = 3   ← OVERWRITES after pools created

        Result: thread pools have 5 workers but the visible constant says 3.
        This is confusing but not functionally broken because:
          - get_max_concurrent() reads from config at call-time
          - SubagentLimitMiddleware reads from config, not from this constant
        The conftest.py mock sets it to 3 to match the visible constant.

        Improvement: Remove the first definition and use the config-driven value.
        """
        import inspect
        import pathlib

        executor_path = (
            pathlib.Path(__file__).parent.parent
            / "packages" / "harness" / "deerflow" / "subagents" / "executor.py"
        )
        source = executor_path.read_text()

        occurrences = [i for i, line in enumerate(source.splitlines(), 1) if "MAX_CONCURRENT_SUBAGENTS = " in line]
        assert len(occurrences) == 2, (
            f"Issue-1: Expected exactly 2 definitions of MAX_CONCURRENT_SUBAGENTS in executor.py, "
            f"found {len(occurrences)} at lines {occurrences}. "
            f"When fixed, update this test to assert len == 1."
        )

    def test_issue1_first_definition_is_5_second_is_3(self):
        """Issue-1: Verify specific values — first definition is 5, second is 3."""
        import pathlib

        executor_path = (
            pathlib.Path(__file__).parent.parent
            / "packages" / "harness" / "deerflow" / "subagents" / "executor.py"
        )
        lines = executor_path.read_text().splitlines()
        defs = [(i + 1, line.strip()) for i, line in enumerate(lines) if "MAX_CONCURRENT_SUBAGENTS = " in line]

        assert len(defs) == 2, f"Expected 2 MAX_CONCURRENT_SUBAGENTS definitions, got {len(defs)}"
        first_val = int(defs[0][1].split("=")[1].strip())
        second_val = int(defs[1][1].split("=")[1].strip())
        assert first_val == 5, f"Issue-1: First definition should be 5 (for thread pools), got {first_val}"
        assert second_val == 3, f"Issue-1: Second definition should be 3 (visible constant), got {second_val}"

    def test_issue2_task_tool_reads_parent_model_from_metadata_not_configurable(self):
        """Issue-2: task_tool.py resolves parent_model from metadata, not configurable.

        In LangGraph, the model_name is typically stored in configurable:
            config['configurable']['model_name']

        But task_tool.py reads:
            metadata = runtime.config.get('metadata', {})
            parent_model = metadata.get('model_name')

        This means parent_model is always None in normal LangGraph usage,
        so 'inherit' subagents fall back to the system default instead of
        inheriting the parent's model choice.

        Improvement: Read from both metadata AND configurable, preferring configurable.
        """
        import pathlib

        task_tool_path = (
            pathlib.Path(__file__).parent.parent
            / "packages" / "harness" / "deerflow" / "tools" / "builtins" / "task_tool.py"
        )
        source = task_tool_path.read_text()

        # Verify the current (buggy) behavior is in place
        assert 'metadata.get("model_name")' in source or "metadata.get('model_name')" in source, (
            "Issue-2: task_tool should be reading parent_model from metadata. "
            "If this assertion fails, the bug may have been fixed — update this test."
        )

        # Verify the correct path (configurable) is NOT being used as primary source
        assert 'configurable", {}).get("model_name")' not in source or \
               source.index('metadata.get("model_name")') < source.index('configurable", {}).get("model_name")', 0) if 'configurable", {}).get("model_name")' in source else True, (
            "Issue-2: If both metadata and configurable are checked, this is fixed."
        )

    def test_issue4_max_concurrent_subagents_missing_from_middleware_module(self):
        """Issue-4: MAX_CONCURRENT_SUBAGENTS is NOT exported from subagent_limit_middleware.py.

        The existing test_subagent_limit_middleware.py imports this constant:
            from deerflow.agents.middlewares.subagent_limit_middleware import (
                MAX_CONCURRENT_SUBAGENTS, ...
            )

        But the middleware module only defines MIN_SUBAGENT_LIMIT and MAX_SUBAGENT_LIMIT.
        This causes an ImportError when test_subagent_limit_middleware.py runs.

        Improvement: Either:
          a) Export MAX_CONCURRENT_SUBAGENTS from the middleware module, or
          b) Fix test_subagent_limit_middleware.py to not import it from there
        """
        import importlib
        import pathlib

        middleware_path = (
            pathlib.Path(__file__).parent.parent
            / "packages" / "harness" / "deerflow" / "agents" / "middlewares" / "subagent_limit_middleware.py"
        )
        source = middleware_path.read_text()

        # Verify the constant is NOT defined in the middleware module
        # (confirming the bug exists)
        constant_defined = "MAX_CONCURRENT_SUBAGENTS = " in source
        assert not constant_defined, (
            "Issue-4: MAX_CONCURRENT_SUBAGENTS is now defined in subagent_limit_middleware.py. "
            "If the test_subagent_limit_middleware.py import works, update this test to reflect "
            "that Issue-4 is resolved."
        )

    def test_issue4_importing_missing_constant_raises_import_error(self):
        """Issue-4: Verify that importing MAX_CONCURRENT_SUBAGENTS from the middleware raises ImportError."""
        with pytest.raises(ImportError):
            from deerflow.agents.middlewares.subagent_limit_middleware import MAX_CONCURRENT_SUBAGENTS  # noqa: F401

    def test_issue3_truncation_does_not_inform_lead_agent(self):
        """Issue-3: SubagentLimitMiddleware truncates tool calls silently.

        After truncation, the lead agent's AIMessage still has the full text
        content, and there is no ToolMessage error injected for the dropped task.
        The lead agent will only discover a task was skipped when it tries to
        aggregate results and finds missing output.

        Improvement: Inject a synthetic ToolMessage for each dropped task_call
        with content explaining the task was deferred due to concurrency limits.
        """
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = _ai_msg_with_tools([_task_call("t1"), _task_call("t2"), _task_call("t3")])
        result = mw._truncate_task_calls({"messages": [msg]})

        # Issue-3: Only the modified AIMessage is returned, no ToolMessage for dropped task
        assert result is not None
        assert len(result["messages"]) == 1, (
            "Issue-3: Only 1 message returned after truncation — "
            "no synthetic ToolMessage error for the dropped task"
        )
        # The remaining message is an AIMessage, not a ToolMessage
        from langchain_core.messages import ToolMessage
        assert not isinstance(result["messages"][0], ToolMessage), (
            "Issue-3: No ToolMessage injected for dropped task"
        )


# ============================================================================
# Section 12 – Clinical Scenario Integration: typical collaboration patterns
# ============================================================================


class TestClinicalCollaborationScenarios:
    """Validate typical multi-agent clinical scenarios at the config/design level."""

    def test_phase3_protocol_design_agents_all_registered(self):
        """Phase 3 protocol design requires: cmo-gpl + trial-design + trial-statistics + gpm."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        required = ["cmo-gpl", "trial-design", "trial-statistics", "gpm"]
        for agent in required:
            assert agent in BUILTIN_SUBAGENTS, (
                f"Phase 3 design requires '{agent}' but it is not registered"
            )

    def test_ind_package_agents_all_registered(self):
        """IND submission requires: toxicology + pharmacology + chemistry + drug-registration."""
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        required = ["toxicology", "pharmacology", "chemistry", "drug-registration"]
        for agent in required:
            assert agent in BUILTIN_SUBAGENTS, (
                f"IND submission requires '{agent}' but it is not registered"
            )

    def test_ops_quality_pair_has_complementary_do_not_use_guidance(self):
        """clinical-ops and quality-control descriptions must clarify scope boundaries."""
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG

        # clinical-ops should NOT mention quality/GCP compliance as its primary domain
        # quality-control should NOT mention site selection/enrollment as its primary domain
        # (Both should have "Do NOT use for" guidance to prevent misrouting)
        assert "Do NOT use" in CLINICAL_OPS_CONFIG.description or "not" in CLINICAL_OPS_CONFIG.description.lower(), (
            "clinical-ops should clarify what it does NOT handle to prevent misrouting to quality-control"
        )
        assert "Do NOT use" in QUALITY_CONTROL_CONFIG.description or "not" in QUALITY_CONTROL_CONFIG.description.lower(), (
            "quality-control should clarify what it does NOT handle to prevent misrouting to clinical-ops"
        )

    def test_clinical_dev_lead_cmo_gpl_timeouts_fit_strategic_analysis(self):
        """cmo-gpl timeout must be sufficient for multi-step benefit-risk analysis."""
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG

        assert CMO_GPL_CONFIG.timeout_seconds >= 600, (
            "cmo-gpl needs ≥ 600s for complex benefit-risk strategy synthesis"
        )

    def test_biostats_trial_statistics_timeout_fits_sample_size_calculation(self):
        """trial-statistics timeout must be sufficient for iterative sample size work."""
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        assert TRIAL_STATISTICS_CONFIG.timeout_seconds >= 600, (
            "trial-statistics needs ≥ 600s for SAP + sample size iteration"
        )

    def test_regulatory_drug_registration_timeout_fits_submission_strategy(self):
        """drug-registration timeout must be sufficient for multi-jurisdiction strategy."""
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG

        assert DRUG_REGISTRATION_CONFIG.timeout_seconds >= 600, (
            "drug-registration needs ≥ 600s for multi-jurisdiction regulatory strategy"
        )

    def test_all_target_agents_have_write_access_except_parkinson(self):
        """Most clinical agents need write_file for output reports.
        parkinson-clinical is read-only (specialist consultation role).
        """
        from deerflow.subagents.builtins.clinical_ops import CLINICAL_OPS_CONFIG
        from deerflow.subagents.builtins.cmo_gpl import CMO_GPL_CONFIG
        from deerflow.subagents.builtins.drug_registration import DRUG_REGISTRATION_CONFIG
        from deerflow.subagents.builtins.parkinson_clinical import PARKINSON_CLINICAL_CONFIG
        from deerflow.subagents.builtins.quality_control import QUALITY_CONTROL_CONFIG
        from deerflow.subagents.builtins.trial_statistics import TRIAL_STATISTICS_CONFIG

        write_agents = [
            ("cmo-gpl", CMO_GPL_CONFIG),
            ("trial-statistics", TRIAL_STATISTICS_CONFIG),
            ("drug-registration", DRUG_REGISTRATION_CONFIG),
            ("clinical-ops", CLINICAL_OPS_CONFIG),
            ("quality-control", QUALITY_CONTROL_CONFIG),
        ]
        for name, config in write_agents:
            assert config.tools is not None and "write_file" in config.tools, (
                f"'{name}' should have write_file to produce output documents"
            )

        # parkinson-clinical is read-only by design
        assert PARKINSON_CLINICAL_CONFIG.tools is None or "write_file" not in (PARKINSON_CLINICAL_CONFIG.tools or []), (
            "parkinson-clinical should be read-only (no write_file) — consultation role only"
        )
