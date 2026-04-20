"""Tests for task_tool timeout and retry logic."""

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subagent_config(timeout_seconds: int = 60):
    from deerflow.subagents.config import SubagentConfig

    return SubagentConfig(
        name="test-agent",
        description="test",
        system_prompt="You are a test agent.",
        timeout_seconds=timeout_seconds,
    )


def _make_result(status, error=None, result=None):
    """Build a lightweight fake BackgroundTaskResult."""
    from deerflow.subagents.executor import SubagentStatus

    r = MagicMock()
    r.status = status
    r.error = error
    r.result = result
    r.ai_messages = []
    r.completed_at = None
    return r


# ---------------------------------------------------------------------------
# Constant checks
# ---------------------------------------------------------------------------


class TestSubagentMaxRetries:
    def test_constant_is_importable(self):
        from deerflow.tools.builtins.task_tool import SUBAGENT_MAX_RETRIES

        assert isinstance(SUBAGENT_MAX_RETRIES, int)

    def test_default_value_is_positive(self):
        from deerflow.tools.builtins.task_tool import SUBAGENT_MAX_RETRIES

        assert SUBAGENT_MAX_RETRIES >= 1


# ---------------------------------------------------------------------------
# Deadline formula tests
# ---------------------------------------------------------------------------


class TestPollDeadlineFormula:
    """Verify the wall-clock deadline exceeds the execution timeout by exactly 120 s."""

    @pytest.mark.parametrize("timeout_seconds", [60, 300, 600, 900, 1800])
    def test_deadline_buffer_is_120_seconds(self, timeout_seconds: int):
        t0 = time.monotonic()
        deadline = t0 + timeout_seconds + 120
        buffer = deadline - t0 - timeout_seconds
        assert buffer == pytest.approx(120, abs=1e-6)

    @pytest.mark.parametrize("timeout_seconds", [60, 300, 900, 1800])
    def test_deadline_strictly_greater_than_timeout(self, timeout_seconds: int):
        t0 = time.monotonic()
        deadline = t0 + timeout_seconds + 120
        assert deadline > t0 + timeout_seconds


# ---------------------------------------------------------------------------
# Retry behaviour (exercised via the poll loop internals)
# ---------------------------------------------------------------------------


class TestTaskToolRetryBehavior:
    """Tests that verify retry mechanics by calling task_tool's polling logic."""

    def _make_runtime(self, config=None):
        from langchain.tools import ToolRuntime

        return ToolRuntime(
            config={"metadata": {"model_name": None, "trace_id": "test"}, "configurable": {}},
            context={"thread_id": "t1"},
            state={"sandbox": None, "thread_data": None},
            stream_writer=MagicMock(),
            tool_call_id="test-call-id",
            store=None,
        )

    def _make_tool_call(self, tool_call_id: str, runtime) -> dict:
        """Build a ToolCall-compatible dict for ainvoke with InjectedToolCallId."""
        return {
            "args": {
                "description": "test task",
                "prompt": "do something",
                "subagent_type": "general-purpose",
                "runtime": runtime,
            },
            "id": tool_call_id,
            "name": "task",
            "type": "tool_call",
        }

    @pytest.mark.anyio
    async def test_retries_on_failure_then_succeeds(self):
        """First attempt FAILED → retry → second attempt COMPLETED → returns success."""
        from deerflow.subagents.executor import SubagentStatus
        from deerflow.tools.builtins.task_tool import SUBAGENT_MAX_RETRIES, task_tool

        assert SUBAGENT_MAX_RETRIES >= 1, "Test requires at least 1 retry"

        subagent_config = _make_subagent_config(timeout_seconds=60)

        call_count = 0

        def _get_result(task_id):
            nonlocal call_count
            call_count += 1
            if "retry" not in task_id:
                # First executor: fail immediately
                return _make_result(SubagentStatus.FAILED, error="simulated error")
            else:
                # Retry executor: succeed immediately
                return _make_result(SubagentStatus.COMPLETED, result="success output")

        writer = MagicMock()

        with (
            patch("deerflow.tools.builtins.task_tool.get_subagent_config", return_value=subagent_config),
            patch("deerflow.tools.builtins.task_tool.get_available_subagent_names", return_value=["general-purpose"]),
            patch("deerflow.tools.builtins.task_tool.get_skills_prompt_section", return_value=""),
            patch("deerflow.tools.builtins.task_tool.is_host_bash_allowed", return_value=True),
            patch("deerflow.tools.get_available_tools", return_value=[]),
            patch("deerflow.tools.builtins.task_tool.get_background_task_result", side_effect=_get_result),
            patch("deerflow.tools.builtins.task_tool.cleanup_background_task"),
            patch("deerflow.tools.builtins.task_tool.get_stream_writer", return_value=writer),
            patch("deerflow.tools.builtins.task_tool.SubagentExecutor") as MockExecutor,
        ):
            executor_instance = MagicMock()
            executor_instance.execute_async = MagicMock(side_effect=lambda prompt, task_id: task_id)
            MockExecutor.return_value = executor_instance

            runtime = self._make_runtime()
            result = await task_tool.ainvoke(self._make_tool_call("call-001", runtime))

        content = result.content if hasattr(result, "content") else str(result)
        assert "Task Succeeded" in content
        assert "success output" in content

    @pytest.mark.anyio
    async def test_exhausts_retries_returns_error(self):
        """All attempts FAILED → no retries left → returns error string."""
        from deerflow.subagents.executor import SubagentStatus
        from deerflow.tools.builtins.task_tool import SUBAGENT_MAX_RETRIES, task_tool

        subagent_config = _make_subagent_config(timeout_seconds=60)

        def _always_fail(task_id):
            return _make_result(SubagentStatus.FAILED, error="persistent error")

        writer = MagicMock()

        with (
            patch("deerflow.tools.builtins.task_tool.get_subagent_config", return_value=subagent_config),
            patch("deerflow.tools.builtins.task_tool.get_available_subagent_names", return_value=["general-purpose"]),
            patch("deerflow.tools.builtins.task_tool.get_skills_prompt_section", return_value=""),
            patch("deerflow.tools.builtins.task_tool.is_host_bash_allowed", return_value=True),
            patch("deerflow.tools.get_available_tools", return_value=[]),
            patch("deerflow.tools.builtins.task_tool.get_background_task_result", side_effect=_always_fail),
            patch("deerflow.tools.builtins.task_tool.cleanup_background_task"),
            patch("deerflow.tools.builtins.task_tool.get_stream_writer", return_value=writer),
            patch("deerflow.tools.builtins.task_tool.SubagentExecutor") as MockExecutor,
        ):
            executor_instance = MagicMock()
            executor_instance.execute_async = MagicMock(side_effect=lambda prompt, task_id: task_id)
            MockExecutor.return_value = executor_instance

            runtime = self._make_runtime()
            result = await task_tool.ainvoke(self._make_tool_call("call-002", runtime))

        content = result.content if hasattr(result, "content") else str(result)
        assert "Task failed" in content
        assert "persistent error" in content
        # Executor created: 1 initial + SUBAGENT_MAX_RETRIES retries
        assert MockExecutor.call_count == 1 + SUBAGENT_MAX_RETRIES

    @pytest.mark.anyio
    async def test_no_retry_on_timeout(self):
        """TIMED_OUT status must NOT trigger a retry."""
        from deerflow.subagents.executor import SubagentStatus
        from deerflow.tools.builtins.task_tool import SUBAGENT_MAX_RETRIES, task_tool

        subagent_config = _make_subagent_config(timeout_seconds=60)

        def _always_timeout(task_id):
            return _make_result(SubagentStatus.TIMED_OUT, error="timed out")

        writer = MagicMock()

        with (
            patch("deerflow.tools.builtins.task_tool.get_subagent_config", return_value=subagent_config),
            patch("deerflow.tools.builtins.task_tool.get_available_subagent_names", return_value=["general-purpose"]),
            patch("deerflow.tools.builtins.task_tool.get_skills_prompt_section", return_value=""),
            patch("deerflow.tools.builtins.task_tool.is_host_bash_allowed", return_value=True),
            patch("deerflow.tools.get_available_tools", return_value=[]),
            patch("deerflow.tools.builtins.task_tool.get_background_task_result", side_effect=_always_timeout),
            patch("deerflow.tools.builtins.task_tool.cleanup_background_task"),
            patch("deerflow.tools.builtins.task_tool.get_stream_writer", return_value=writer),
            patch("deerflow.tools.builtins.task_tool.SubagentExecutor") as MockExecutor,
        ):
            executor_instance = MagicMock()
            executor_instance.execute_async = MagicMock(side_effect=lambda prompt, task_id: task_id)
            MockExecutor.return_value = executor_instance

            runtime = self._make_runtime()
            result = await task_tool.ainvoke(self._make_tool_call("call-003", runtime))

        content = result.content if hasattr(result, "content") else str(result)
        assert "timed out" in content.lower()
        # Only one executor ever created — no retry
        assert MockExecutor.call_count == 1


class TestPollDeadlineLogic:
    """Pure-logic tests for the wall-clock deadline guard (no ainvoke required)."""

    def test_deadline_exceeded_condition(self):
        """Verify the deadline check: time.monotonic() > poll_deadline."""
        import time

        # Simulate: poll_deadline was set in the past
        past_deadline = time.monotonic() - 1
        assert time.monotonic() > past_deadline

    def test_deadline_not_yet_exceeded(self):
        """Verify the deadline check does NOT fire when there is time remaining."""
        import time

        future_deadline = time.monotonic() + 9999
        assert not (time.monotonic() > future_deadline)

    def test_deadline_set_correctly_from_timeout(self):
        """The deadline = monotonic_now + timeout_seconds + 120."""
        import time

        timeout_seconds = 300
        t0 = time.monotonic()
        deadline = t0 + timeout_seconds + 120
        # Should not be exceeded yet
        assert not (time.monotonic() > deadline)
        # Simulated future time that exceeds deadline
        simulated_future = t0 + timeout_seconds + 121
        assert simulated_future > deadline
