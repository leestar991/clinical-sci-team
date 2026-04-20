"""Tests for SubagentLimitMiddleware."""

import pytest
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.subagent_limit_middleware import (
    MAX_CONCURRENT_SUBAGENTS,
    MAX_SUBAGENT_LIMIT,
    MIN_SUBAGENT_LIMIT,
    SubagentLimitMiddleware,
    _clamp_subagent_limit,
    _is_complete_task_call,
)


def _make_runtime():
    runtime = MagicMock()
    runtime.context = {"thread_id": "test-thread"}
    return runtime


def _task_call(task_id="call_1", description="do task", prompt="do something", subagent_type="general-purpose"):
    """Return a well-formed task() tool call with all required fields."""
    return {
        "name": "task",
        "id": task_id,
        "args": {
            "description": description,
            "prompt": prompt,
            "subagent_type": subagent_type,
        },
    }


def _incomplete_task_call(task_id="call_bad", **kwargs):
    """Return a task() tool call with the given args (allows omitting required fields)."""
    return {"name": "task", "id": task_id, "args": kwargs}


def _other_call(name="bash", call_id="call_other"):
    return {"name": name, "id": call_id, "args": {}}


class TestClampSubagentLimit:
    def test_below_min_clamped_to_min(self):
        assert _clamp_subagent_limit(0) == MIN_SUBAGENT_LIMIT
        assert _clamp_subagent_limit(1) == MIN_SUBAGENT_LIMIT

    def test_above_max_clamped_to_max(self):
        assert _clamp_subagent_limit(10) == MAX_SUBAGENT_LIMIT
        assert _clamp_subagent_limit(100) == MAX_SUBAGENT_LIMIT

    def test_within_range_unchanged(self):
        assert _clamp_subagent_limit(2) == 2
        assert _clamp_subagent_limit(3) == 3
        assert _clamp_subagent_limit(4) == 4


class TestSubagentLimitMiddlewareInit:
    def test_default_max_concurrent(self):
        mw = SubagentLimitMiddleware()
        assert mw.max_concurrent == MAX_CONCURRENT_SUBAGENTS

    def test_custom_max_concurrent_clamped(self):
        mw = SubagentLimitMiddleware(max_concurrent=1)
        assert mw.max_concurrent == MIN_SUBAGENT_LIMIT

        mw = SubagentLimitMiddleware(max_concurrent=10)
        assert mw.max_concurrent == MAX_SUBAGENT_LIMIT


class TestIsCompleteTaskCall:
    def test_all_required_fields_present(self):
        tc = _task_call()
        assert _is_complete_task_call(tc) is True

    def test_empty_args_returns_false(self):
        tc = {"name": "task", "id": "x", "args": {}}
        assert _is_complete_task_call(tc) is False

    def test_missing_description_returns_false(self):
        tc = _incomplete_task_call(prompt="do something", subagent_type="general-purpose")
        assert _is_complete_task_call(tc) is False

    def test_missing_prompt_returns_false(self):
        tc = _incomplete_task_call(description="do task", subagent_type="general-purpose")
        assert _is_complete_task_call(tc) is False

    def test_missing_subagent_type_returns_false(self):
        tc = _incomplete_task_call(description="do task", prompt="do something")
        assert _is_complete_task_call(tc) is False

    def test_empty_string_field_returns_false(self):
        tc = _incomplete_task_call(description="", prompt="do something", subagent_type="general-purpose")
        assert _is_complete_task_call(tc) is False

    def test_none_args_returns_false(self):
        tc = {"name": "task", "id": "x", "args": None}
        assert _is_complete_task_call(tc) is False


class TestSanitizeTaskCalls:
    def test_no_messages_returns_none(self):
        mw = SubagentLimitMiddleware()
        assert mw._sanitize_task_calls({"messages": []}) is None

    def test_missing_messages_returns_none(self):
        mw = SubagentLimitMiddleware()
        assert mw._sanitize_task_calls({}) is None

    def test_last_message_not_ai_returns_none(self):
        mw = SubagentLimitMiddleware()
        state = {"messages": [HumanMessage(content="hello")]}
        assert mw._sanitize_task_calls(state) is None

    def test_ai_no_tool_calls_returns_none(self):
        mw = SubagentLimitMiddleware()
        state = {"messages": [AIMessage(content="thinking...")]}
        assert mw._sanitize_task_calls(state) is None

    def test_task_calls_within_limit_returns_none(self):
        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        assert mw._sanitize_task_calls({"messages": [msg]}) is None

    def test_task_calls_exceeding_limit_truncated(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3"), _task_call("t4")],
        )
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        task_calls = [tc for tc in updated_msg.tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2
        assert task_calls[0]["id"] == "t1"
        assert task_calls[1]["id"] == "t2"

    def test_non_task_calls_preserved(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = AIMessage(
            content="",
            tool_calls=[
                _other_call("bash", "b1"),
                _task_call("t1"),
                _task_call("t2"),
                _task_call("t3"),
                _other_call("read", "r1"),
            ],
        )
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        names = [tc["name"] for tc in updated_msg.tool_calls]
        assert "bash" in names
        assert "read" in names
        task_calls = [tc for tc in updated_msg.tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2

    def test_only_non_task_calls_returns_none(self):
        mw = SubagentLimitMiddleware()
        msg = AIMessage(
            content="",
            tool_calls=[_other_call("bash", "b1"), _other_call("read", "r1")],
        )
        assert mw._sanitize_task_calls({"messages": [msg]}) is None


class TestIncompleteTaskCallDropping:
    def test_empty_args_task_call_dropped(self):
        mw = SubagentLimitMiddleware()
        msg = AIMessage(
            content="",
            tool_calls=[_incomplete_task_call("bad", **{})],
        )
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 0

    def test_incomplete_call_dropped_complete_call_kept(self):
        mw = SubagentLimitMiddleware()
        good = _task_call("good")
        bad = _incomplete_task_call("bad")
        msg = AIMessage(content="", tool_calls=[good, bad])
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 1
        assert task_calls[0]["id"] == "good"

    def test_all_incomplete_calls_dropped(self):
        mw = SubagentLimitMiddleware()
        calls = [
            _incomplete_task_call("b1", prompt="p1"),
            _incomplete_task_call("b2", description="d2"),
            _incomplete_task_call("b3", subagent_type="general-purpose"),
        ]
        msg = AIMessage(content="", tool_calls=calls)
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 0

    def test_incomplete_dropped_before_concurrency_cap_applied(self):
        """Incomplete calls should be dropped first; cap only applies to remaining valid calls."""
        mw = SubagentLimitMiddleware(max_concurrent=2)
        calls = [
            _task_call("t1"),
            _task_call("t2"),
            _incomplete_task_call("bad"),  # incomplete — should be dropped
        ]
        msg = AIMessage(content="", tool_calls=calls)
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        # After dropping incomplete: 2 valid, cap=2 → no excess truncation
        assert len(task_calls) == 2
        assert task_calls[0]["id"] == "t1"
        assert task_calls[1]["id"] == "t2"

    def test_non_task_calls_preserved_when_incomplete_dropped(self):
        mw = SubagentLimitMiddleware()
        msg = AIMessage(
            content="",
            tool_calls=[
                _other_call("bash", "b1"),
                _incomplete_task_call("bad"),
                _other_call("read", "r1"),
            ],
        )
        result = mw._sanitize_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        names = [tc["name"] for tc in updated_msg.tool_calls]
        assert "bash" in names
        assert "read" in names
        task_calls = [tc for tc in updated_msg.tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 0


class TestAfterModel:
    def test_delegates_to_sanitize(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        result = mw.after_model({"messages": [msg]}, runtime)
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2

    @pytest.mark.anyio
    async def test_aafter_model_delegates_to_sanitize(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        result = await mw.aafter_model({"messages": [msg]}, runtime)
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2
