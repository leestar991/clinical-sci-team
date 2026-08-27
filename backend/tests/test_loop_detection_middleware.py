"""Tests for LoopDetectionMiddleware."""

import copy
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool as as_tool
from pydantic import PrivateAttr

from deerflow.agents.middlewares.loop_detection_middleware import (
    _HARD_STOP_MSG,
    _MAX_CUMULATIVE_HASHES,
    _MAX_PENDING_WARNINGS_PER_RUN,
    LoopDetectionMiddleware,
    _hash_tool_calls,
)
from deerflow.config.loop_detection_config import LoopDetectionConfig


def _make_runtime(thread_id="test-thread", run_id="test-run", task_id=None):
    """Build a minimal Runtime mock with context."""
    runtime = MagicMock()
    runtime.context = {"thread_id": thread_id, "run_id": run_id}
    if task_id is not None:
        runtime.context["task_id"] = task_id
    return runtime


def _pending_key(thread_id="test-thread", run_id="test-run"):
    return (thread_id, run_id)


def _make_request(messages, runtime):
    """Build a minimal ModelRequest stand-in for wrap_model_call tests."""
    request = MagicMock()
    request.messages = list(messages)
    request.runtime = runtime
    request.override = lambda **updates: _override_request(request, updates)
    return request


def _override_request(request, updates):
    """Mimic ModelRequest.override(): return a copy with fields replaced."""
    new = MagicMock()
    new.messages = updates.get("messages", request.messages)
    new.runtime = updates.get("runtime", request.runtime)
    new.override = lambda **u: _override_request(new, u)
    return new


def _capture_handler():
    """Build a sync handler that records the request it was called with."""
    captured: list = []

    def handler(req):
        captured.append(req)
        return MagicMock()

    return captured, handler


class _CapturingFakeMessagesListChatModel(FakeMessagesListChatModel):
    """Fake chat model that records each model request's messages."""

    _seen_messages: list[list[Any]] = PrivateAttr(default_factory=list)

    @property
    def seen_messages(self) -> list[list[Any]]:
        return self._seen_messages

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Runnable:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen_messages.append(list(messages))
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


def _make_state(tool_calls=None, content=""):
    """Build a minimal AgentState dict with an AIMessage.

    Deep-copies *content* when it is mutable (e.g. list) so that
    successive calls never share the same object reference.
    """
    safe_content = copy.deepcopy(content) if isinstance(content, list) else content
    msg = AIMessage(content=safe_content, tool_calls=tool_calls or [])
    return {"messages": [msg]}


def _bash_call(cmd="ls"):
    return {"name": "bash", "id": f"call_{cmd}", "args": {"command": cmd}}


class TestHashToolCalls:
    def test_same_calls_same_hash(self):
        a = _hash_tool_calls([_bash_call("ls")])
        b = _hash_tool_calls([_bash_call("ls")])
        assert a == b

    def test_different_calls_different_hash(self):
        a = _hash_tool_calls([_bash_call("ls")])
        b = _hash_tool_calls([_bash_call("pwd")])
        assert a != b

    def test_order_independent(self):
        a = _hash_tool_calls([_bash_call("ls"), {"name": "read_file", "args": {"path": "/tmp"}}])
        b = _hash_tool_calls([{"name": "read_file", "args": {"path": "/tmp"}}, _bash_call("ls")])
        assert a == b

    def test_empty_calls(self):
        h = _hash_tool_calls([])
        assert isinstance(h, str)
        assert len(h) > 0

    def test_stringified_dict_args_match_dict_args(self):
        dict_call = {
            "name": "read_file",
            "args": {"path": "/tmp/demo.py", "start_line": "1", "end_line": "150"},
        }
        string_call = {
            "name": "read_file",
            "args": '{"path":"/tmp/demo.py","start_line":"1","end_line":"150"}',
        }

        assert _hash_tool_calls([dict_call]) == _hash_tool_calls([string_call])

    def test_reversed_read_file_range_matches_forward_range(self):
        forward_call = {
            "name": "read_file",
            "args": {"path": "/tmp/demo.py", "start_line": 10, "end_line": 300},
        }
        reversed_call = {
            "name": "read_file",
            "args": {"path": "/tmp/demo.py", "start_line": 300, "end_line": 10},
        }

        assert _hash_tool_calls([forward_call]) == _hash_tool_calls([reversed_call])

    def test_stringified_non_dict_args_do_not_crash(self):
        non_dict_json_call = {"name": "bash", "args": '"echo hello"'}
        plain_string_call = {"name": "bash", "args": "echo hello"}

        json_hash = _hash_tool_calls([non_dict_json_call])
        plain_hash = _hash_tool_calls([plain_string_call])

        assert isinstance(json_hash, str)
        assert isinstance(plain_hash, str)
        assert json_hash
        assert plain_hash

    def test_grep_pattern_affects_hash(self):
        grep_foo = {"name": "grep", "args": {"path": "/tmp", "pattern": "foo"}}
        grep_bar = {"name": "grep", "args": {"path": "/tmp", "pattern": "bar"}}

        assert _hash_tool_calls([grep_foo]) != _hash_tool_calls([grep_bar])

    def test_glob_pattern_affects_hash(self):
        glob_py = {"name": "glob", "args": {"path": "/tmp", "pattern": "*.py"}}
        glob_ts = {"name": "glob", "args": {"path": "/tmp", "pattern": "*.ts"}}

        assert _hash_tool_calls([glob_py]) != _hash_tool_calls([glob_ts])

    def test_write_file_content_affects_hash(self):
        v1 = {"name": "write_file", "args": {"path": "/tmp/a.py", "content": "v1"}}
        v2 = {"name": "write_file", "args": {"path": "/tmp/a.py", "content": "v2"}}
        assert _hash_tool_calls([v1]) != _hash_tool_calls([v2])

    def test_apply_json_patches_content_affects_hash(self):
        """两批不同的 patch 必须是两个哈希。

        该工具的 args 只有 `path` 和 `patches`，salient-field 回退只看 `path`，会把
        「逐条应用 QC 结论」的连续调用折叠成同一个哈希，从而误判成死循环。
        """
        a = {
            "name": "apply_json_patches",
            "args": {"path": "/mnt/user-data/workspace/j.json", "expected_hash": "abc", "patches": [{"pointer": "/a/conclusion", "op": "replace", "value": "符合"}]},
        }
        b = {
            "name": "apply_json_patches",
            "args": {"path": "/mnt/user-data/workspace/j.json", "expected_hash": "def", "patches": [{"pointer": "/b/conclusion", "op": "replace", "value": "不符合"}]},
        }
        assert _hash_tool_calls([a]) != _hash_tool_calls([b])

    def test_identical_apply_json_patches_calls_still_collide(self):
        """真正重复的同一批 patch 仍必须被识别为重复（否则熔断失效）。"""
        call = {
            "name": "apply_json_patches",
            "args": {"path": "/mnt/user-data/workspace/j.json", "expected_hash": "abc", "patches": [{"pointer": "/a", "op": "remove"}]},
        }
        assert _hash_tool_calls([call]) == _hash_tool_calls([dict(call)])

    def test_str_replace_content_affects_hash(self):
        a = {
            "name": "str_replace",
            "args": {"path": "/tmp/a.py", "old_str": "foo", "new_str": "bar"},
        }
        b = {
            "name": "str_replace",
            "args": {"path": "/tmp/a.py", "old_str": "foo", "new_str": "baz"},
        }
        assert _hash_tool_calls([a]) != _hash_tool_calls([b])


class TestLoopDetection:
    def test_no_tool_calls_returns_none(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        state = {"messages": [AIMessage(content="hello")]}
        result = mw._apply(state, runtime)
        assert result is None

    def test_below_threshold_returns_none(self):
        mw = LoopDetectionMiddleware(warn_threshold=3)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # First two identical calls — no warning
        for _ in range(2):
            result = mw._apply(_make_state(tool_calls=call), runtime)
            assert result is None

    def test_warn_at_threshold_queues_but_does_not_mutate_state(self):
        """At warn threshold, ``after_model`` enqueues but returns None.

        Detection observes the just-emitted AIMessage(tool_calls=...). The
        tools node hasn't run yet, so injecting any non-tool message here
        would split the assistant's tool_calls from their ToolMessage
        responses and break OpenAI/Moonshot pairing. The warning is
        delivered later from ``wrap_model_call``.
        """
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)

        # Third identical call triggers warning detection.
        result = mw._apply(_make_state(tool_calls=call), runtime)
        # Detection must not mutate state — the AIMessage with tool_calls is
        # left untouched so the tools node runs normally.
        assert result is None
        # ...but a warning is queued for the next model call.
        assert mw._pending_warnings[_pending_key()]
        assert "LOOP DETECTED" in mw._pending_warnings[_pending_key()][0]

    def test_warn_injected_at_next_model_call(self):
        """``wrap_model_call`` appends a HumanMessage(loop_warning) to the
        outgoing messages — *after* every existing message — so that the
        AIMessage(tool_calls=...) -> ToolMessage(...) pairing stays intact.
        """
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]
        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        # Build the messages the agent runtime would assemble for the next
        # turn: prior AIMessage(tool_calls), its ToolMessage responses, ...
        ai_msg = AIMessage(content="", tool_calls=call)
        tool_msg = ToolMessage(content="ok", tool_call_id=call[0]["id"], name="bash")
        request = _make_request([ai_msg, tool_msg], runtime)

        captured, handler = _capture_handler()
        mw.wrap_model_call(request, handler)

        sent = captured[0].messages
        # AIMessage and ToolMessage stay in order, untouched.
        assert sent[0] is ai_msg
        assert sent[1] is tool_msg
        # HumanMessage(warning) appears AFTER the ToolMessage — pairing intact.
        assert isinstance(sent[2], HumanMessage)
        assert sent[2].name == "loop_warning"
        assert "LOOP DETECTED" in sent[2].content

    def test_warn_queue_drained_after_injection(self):
        """A queued warning must be emitted exactly once per detection event."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]
        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        request = _make_request([AIMessage(content="hi")], runtime)
        captured, handler = _capture_handler()

        # First call: warning is appended.
        mw.wrap_model_call(request, handler)
        first = captured[0].messages
        assert any(isinstance(m, HumanMessage) for m in first)

        # Subsequent call without new detection: no warning re-emitted.
        request2 = _make_request([AIMessage(content="hi")], runtime)
        mw.wrap_model_call(request2, handler)
        second = captured[1].messages
        assert not any(isinstance(m, HumanMessage) for m in second)

    def test_warn_queue_scoped_by_run_id(self):
        """A warning queued for one run must not be injected into another run."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime_a = _make_runtime(run_id="run-A")
        runtime_b = _make_runtime(run_id="run-B")
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime_a)

        request_b = _make_request([AIMessage(content="hi")], runtime_b)
        captured, handler = _capture_handler()
        mw.wrap_model_call(request_b, handler)
        assert not any(isinstance(m, HumanMessage) for m in captured[0].messages)
        assert mw._pending_warnings.get(_pending_key(run_id="run-A"))

        request_a = _make_request([AIMessage(content="hi")], runtime_a)
        mw.wrap_model_call(request_a, handler)
        assert any(isinstance(message, HumanMessage) and message.name == "loop_warning" for message in captured[1].messages)

    def test_missing_run_id_uses_default_pending_scope(self):
        """When runtime has no run_id, warning handling falls back to the default run scope."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = MagicMock()
        runtime.context = {"thread_id": "test-thread"}
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        assert mw._pending_warnings.get(_pending_key(run_id="default"))

        request = _make_request([AIMessage(content="hi")], runtime)
        captured, handler = _capture_handler()
        mw.wrap_model_call(request, handler)

        loop_warnings = [message for message in captured[0].messages if isinstance(message, HumanMessage) and message.name == "loop_warning"]
        assert len(loop_warnings) == 1
        assert "LOOP DETECTED" in loop_warnings[0].content
        assert not mw._pending_warnings.get(_pending_key(run_id="default"))

    def test_before_agent_clears_stale_pending_warnings_for_thread(self):
        """Starting a new run drops stale warnings from prior runs in the same thread."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime_a = _make_runtime(run_id="run-A")
        runtime_b = _make_runtime(run_id="run-B")
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime_a)

        assert mw._pending_warnings.get(_pending_key(run_id="run-A"))
        mw.before_agent({"messages": []}, runtime_b)
        assert not mw._pending_warnings.get(_pending_key(run_id="run-A"))

    def test_after_agent_clears_current_run_pending_warnings(self):
        """Run cleanup should drop warnings that never reached wrap_model_call."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        assert mw._pending_warnings.get(_pending_key())
        mw.after_agent({"messages": []}, runtime)
        assert not mw._pending_warnings.get(_pending_key())

    def test_multiple_pending_warnings_are_merged_into_one_message(self):
        """Edge-case drains should produce one loop_warning prompt message."""
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        mw._pending_warnings[_pending_key()] = ["first warning", "second warning", "first warning"]
        request = _make_request([AIMessage(content="hi")], runtime)
        captured, handler = _capture_handler()

        mw.wrap_model_call(request, handler)

        loop_warnings = [message for message in captured[0].messages if isinstance(message, HumanMessage) and message.name == "loop_warning"]
        assert len(loop_warnings) == 1
        assert loop_warnings[0].content == "first warning\n\nsecond warning"

    def test_warn_only_queued_once_per_hash(self):
        """Same hash repeated past the threshold should warn only once."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # First two — no warning
        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)

        # Third — warning queued
        mw._apply(_make_state(tool_calls=call), runtime)
        assert len(mw._pending_warnings[_pending_key()]) == 1

        # Fourth — already warned for this hash, no additional enqueue.
        mw._apply(_make_state(tool_calls=call), runtime)
        assert len(mw._pending_warnings[_pending_key()]) == 1

    def test_hard_stop_at_limit(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        # Fourth call triggers hard stop
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 1
        # Hard stop strips tool_calls
        assert isinstance(msgs[0], AIMessage)
        assert msgs[0].tool_calls == []
        assert _HARD_STOP_MSG in msgs[0].content

    def test_different_calls_dont_trigger(self):
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = _make_runtime()

        # Each call is different
        for i in range(10):
            result = mw._apply(_make_state(tool_calls=[_bash_call(f"cmd_{i}")]), runtime)
            assert result is None

    def test_window_sliding(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, window_size=5)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # Fill with 2 identical calls
        mw._apply(_make_state(tool_calls=call), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)

        # Push them out of the window with different calls
        for i in range(5):
            mw._apply(_make_state(tool_calls=[_bash_call(f"other_{i}")]), runtime)

        # Now the original call should be fresh again — no warning
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is None

    def test_reset_clears_state(self):
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        mw._apply(_make_state(tool_calls=call), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)

        # Would trigger warning, but reset first
        mw.reset()
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is None
        assert not mw._pending_warnings.get(_pending_key())

    def test_non_ai_message_ignored(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        state = {"messages": [SystemMessage(content="hello")]}
        result = mw._apply(state, runtime)
        assert result is None

    def test_empty_messages_ignored(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        result = mw._apply({"messages": []}, runtime)
        assert result is None

    def test_thread_id_from_runtime_context(self):
        """Thread ID should come from runtime.context, not state."""
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime_a = _make_runtime("thread-A")
        runtime_b = _make_runtime("thread-B")
        call = [_bash_call("ls")]

        # One call on thread A
        mw._apply(_make_state(tool_calls=call), runtime_a)
        # One call on thread B
        mw._apply(_make_state(tool_calls=call), runtime_b)

        # Second call on thread A — queues warning under thread-A only.
        mw._apply(_make_state(tool_calls=call), runtime_a)
        assert mw._pending_warnings.get(_pending_key("thread-A"))
        assert "LOOP DETECTED" in mw._pending_warnings[_pending_key("thread-A")][0]
        assert not mw._pending_warnings.get(_pending_key("thread-B"))

        # Second call on thread B — independent queue.
        mw._apply(_make_state(tool_calls=call), runtime_b)
        assert mw._pending_warnings.get(_pending_key("thread-B"))
        assert "LOOP DETECTED" in mw._pending_warnings[_pending_key("thread-B")][0]

    def test_lru_eviction(self):
        """Old threads should be evicted when max_tracked_threads is exceeded."""
        mw = LoopDetectionMiddleware(warn_threshold=2, max_tracked_threads=3)
        call = [_bash_call("ls")]

        # Fill up 3 threads
        for i in range(3):
            runtime = _make_runtime(f"thread-{i}")
            mw._apply(_make_state(tool_calls=call), runtime)

        # Add a 4th thread — should evict thread-0
        runtime_new = _make_runtime("thread-new")
        mw._apply(_make_state(tool_calls=call), runtime_new)

        assert "thread-0" not in mw._history
        assert "thread-0" not in mw._tool_freq
        assert "thread-0" not in mw._tool_freq_warned
        assert "thread-new" in mw._history
        assert len(mw._history) == 3

    def test_warned_hashes_are_pruned_to_sliding_window(self):
        """A long-lived thread should not keep every historical warned hash."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=100, window_size=4)
        runtime = _make_runtime()

        for i in range(12):
            call = [_bash_call(f"cmd_{i}")]
            mw._apply(_make_state(tool_calls=call), runtime)
            mw._apply(_make_state(tool_calls=call), runtime)

        assert len(mw._history["test-thread"]) <= 4
        assert set(mw._warned["test-thread"]).issubset(set(mw._history["test-thread"]))
        assert len(mw._warned["test-thread"]) <= 4

    def test_pending_warning_keys_are_capped(self):
        """Abnormal same-thread runs cannot grow pending-warning keys forever."""
        mw = LoopDetectionMiddleware(warn_threshold=2, max_tracked_threads=2)

        for i in range(10):
            runtime = _make_runtime(thread_id="same-thread", run_id=f"run-{i}")
            mw._queue_pending_warning(runtime, f"warning-{i}")

        assert len(mw._pending_warnings) == mw._max_pending_warning_keys
        assert len(mw._pending_warning_touch_order) == mw._max_pending_warning_keys
        assert _pending_key("same-thread", "run-9") in mw._pending_warnings

    def test_pending_warning_list_is_capped_and_deduped(self):
        """One run cannot accumulate an unbounded warning list."""
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()

        for i in range(_MAX_PENDING_WARNINGS_PER_RUN + 4):
            mw._queue_pending_warning(runtime, f"warning-{i}")
        mw._queue_pending_warning(runtime, f"warning-{_MAX_PENDING_WARNINGS_PER_RUN + 3}")

        warnings = mw._pending_warnings[_pending_key()]
        assert len(warnings) == _MAX_PENDING_WARNINGS_PER_RUN
        assert warnings == [f"warning-{i}" for i in range(4, _MAX_PENDING_WARNINGS_PER_RUN + 4)]

    def test_pending_warning_touch_order_cleared_with_pending_key(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        mw._queue_pending_warning(runtime, "warning")

        mw.after_agent({"messages": []}, runtime)

        assert mw._pending_warnings == {}
        assert mw._pending_warning_touch_order == OrderedDict()

    def test_thread_safe_mutations(self):
        """Verify lock is used for mutations (basic structural test)."""
        mw = LoopDetectionMiddleware()
        # The middleware should have a lock attribute
        assert hasattr(mw, "_lock")
        assert isinstance(mw._lock, type(mw._lock))

    def test_fallback_thread_id_when_missing(self):
        """When runtime context has no thread_id, should use 'default'."""
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = MagicMock()
        runtime.context = {}
        call = [_bash_call("ls")]

        mw._apply(_make_state(tool_calls=call), runtime)
        assert "default" in mw._history


class TestLoopDetectionAgentGraphIntegration:
    def test_loop_warning_is_transient_in_real_agent_graph(self):
        """after_model queues the warning; wrap_model_call injects it request-only."""

        @as_tool
        def bash(command: str) -> str:
            """Run a fake shell command."""
            return f"ran: {command}"

        repeated_calls = [[{"name": "bash", "id": f"call_ls_{i}", "args": {"command": "ls"}}] for i in range(3)]
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        model = _CapturingFakeMessagesListChatModel(
            responses=[
                AIMessage(content="", tool_calls=repeated_calls[0]),
                AIMessage(content="", tool_calls=repeated_calls[1]),
                AIMessage(content="", tool_calls=repeated_calls[2]),
                AIMessage(content="final answer"),
            ],
        )
        graph = create_agent(model=model, tools=[bash], middleware=[mw])

        result = graph.invoke(
            {"messages": [("user", "inspect the directory")]},
            context={"thread_id": "integration-thread", "run_id": "integration-run"},
            config={"recursion_limit": 20},
        )

        assert len(model.seen_messages) == 4
        loop_warnings_by_call = [[message for message in messages if isinstance(message, HumanMessage) and message.name == "loop_warning"] for messages in model.seen_messages]
        assert loop_warnings_by_call[0] == []
        assert loop_warnings_by_call[1] == []
        assert loop_warnings_by_call[2] == []
        assert len(loop_warnings_by_call[3]) == 1
        assert "LOOP DETECTED" in loop_warnings_by_call[3][0].content

        fourth_request = model.seen_messages[3]
        assert isinstance(fourth_request[-2], ToolMessage)
        assert fourth_request[-2].tool_call_id == "call_ls_2"
        assert fourth_request[-1] is loop_warnings_by_call[3][0]

        persisted_loop_warnings = [message for message in result["messages"] if isinstance(message, HumanMessage) and message.name == "loop_warning"]
        assert persisted_loop_warnings == []
        assert result["messages"][-1].content == "final answer"
        assert mw._pending_warnings == {}
        assert mw._pending_warning_touch_order == OrderedDict()

    @pytest.mark.asyncio
    async def test_loop_warning_is_transient_in_async_agent_graph(self):
        """awrap_model_call injects loop_warning request-only in async graph runs."""

        @as_tool
        async def bash(command: str) -> str:
            """Run a fake shell command."""
            return f"ran: {command}"

        repeated_calls = [[{"name": "bash", "id": f"call_async_ls_{i}", "args": {"command": "ls"}}] for i in range(3)]
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        model = _CapturingFakeMessagesListChatModel(
            responses=[
                AIMessage(content="", tool_calls=repeated_calls[0]),
                AIMessage(content="", tool_calls=repeated_calls[1]),
                AIMessage(content="", tool_calls=repeated_calls[2]),
                AIMessage(content="async final answer"),
            ],
        )
        graph = create_agent(model=model, tools=[bash], middleware=[mw])

        result = await graph.ainvoke(
            {"messages": [("user", "inspect the directory asynchronously")]},
            context={"thread_id": "async-integration-thread", "run_id": "async-integration-run"},
            config={"recursion_limit": 20},
        )

        assert len(model.seen_messages) == 4
        loop_warnings_by_call = [[message for message in messages if isinstance(message, HumanMessage) and message.name == "loop_warning"] for messages in model.seen_messages]
        assert loop_warnings_by_call[0] == []
        assert loop_warnings_by_call[1] == []
        assert loop_warnings_by_call[2] == []
        assert len(loop_warnings_by_call[3]) == 1
        assert "LOOP DETECTED" in loop_warnings_by_call[3][0].content

        fourth_request = model.seen_messages[3]
        assert isinstance(fourth_request[-2], ToolMessage)
        assert fourth_request[-2].tool_call_id == "call_async_ls_2"
        assert fourth_request[-1] is loop_warnings_by_call[3][0]

        persisted_loop_warnings = [message for message in result["messages"] if isinstance(message, HumanMessage) and message.name == "loop_warning"]
        assert persisted_loop_warnings == []
        assert result["messages"][-1].content == "async final answer"
        assert mw._pending_warnings == {}
        assert mw._pending_warning_touch_order == OrderedDict()


class TestAppendText:
    """Unit tests for LoopDetectionMiddleware._append_text."""

    def test_none_content_returns_text(self):
        result = LoopDetectionMiddleware._append_text(None, "hello")
        assert result == "hello"

    def test_str_content_concatenates(self):
        result = LoopDetectionMiddleware._append_text("existing", "appended")
        assert result == "existing\n\nappended"

    def test_empty_str_content_concatenates(self):
        result = LoopDetectionMiddleware._append_text("", "appended")
        assert result == "\n\nappended"

    def test_list_content_appends_text_block(self):
        """List content (e.g. Anthropic thinking mode) should get a new text block."""
        content = [
            {"type": "thinking", "text": "Let me think..."},
            {"type": "text", "text": "Here is my answer"},
        ]
        result = LoopDetectionMiddleware._append_text(content, "stop msg")
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0] == content[0]
        assert result[1] == content[1]
        assert result[2] == {"type": "text", "text": "\n\nstop msg"}

    def test_empty_list_content_appends_text_block(self):
        result = LoopDetectionMiddleware._append_text([], "stop msg")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] == {"type": "text", "text": "\n\nstop msg"}

    def test_unexpected_type_coerced_to_str(self):
        """Unexpected content types should be coerced to str as a fallback."""
        result = LoopDetectionMiddleware._append_text(42, "stop msg")
        assert isinstance(result, str)
        assert result == "42\n\nstop msg"

    def test_list_content_not_mutated_in_place(self):
        """_append_text must not modify the original list."""
        original = [{"type": "text", "text": "hello"}]
        result = LoopDetectionMiddleware._append_text(original, "appended")
        assert len(original) == 1  # original unchanged
        assert len(result) == 2  # new list has the appended block


class TestHardStopWithListContent:
    """Regression tests: hard stop must not crash when AIMessage.content is a list."""

    def test_hard_stop_with_list_content(self):
        """Hard stop on list content should not raise TypeError (regression)."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # Build state with list content (e.g. Anthropic thinking mode)
        list_content = [
            {"type": "thinking", "text": "Let me think..."},
            {"type": "text", "text": "I'll run ls"},
        ]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call, content=list_content), runtime)

        # Fourth call triggers hard stop — must not raise TypeError
        result = mw._apply(_make_state(tool_calls=call, content=list_content), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg, AIMessage)
        assert msg.tool_calls == []
        # Content should remain a list with the stop message appended
        assert isinstance(msg.content, list)
        assert len(msg.content) == 3
        assert msg.content[2]["type"] == "text"
        assert _HARD_STOP_MSG in msg.content[2]["text"]

    def test_hard_stop_with_none_content(self):
        """Hard stop on None content should produce a plain string."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call), runtime)

        # Fourth call with default empty-string content
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg.content, str)
        assert _HARD_STOP_MSG in msg.content

    def test_hard_stop_with_str_content(self):
        """Hard stop on str content should concatenate the stop message."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(3):
            mw._apply(_make_state(tool_calls=call, content="thinking..."), runtime)

        result = mw._apply(_make_state(tool_calls=call, content="thinking..."), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg.content, str)
        assert msg.content.startswith("thinking...")
        assert _HARD_STOP_MSG in msg.content

    def test_hard_stop_clears_raw_tool_call_metadata(self):
        """Forced-stop messages must not retain provider-level raw tool-call payloads."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        def _make_provider_state():
            return {
                "messages": [
                    AIMessage(
                        content="thinking...",
                        tool_calls=call,
                        additional_kwargs={
                            "tool_calls": [
                                {
                                    "id": "call_ls",
                                    "type": "function",
                                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                                    "thought_signature": "sig-1",
                                }
                            ],
                            "function_call": {"name": "bash", "arguments": '{"command":"ls"}'},
                        },
                        response_metadata={"finish_reason": "tool_calls"},
                    )
                ]
            }

        for _ in range(3):
            mw._apply(_make_provider_state(), runtime)

        result = mw._apply(_make_provider_state(), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert msg.tool_calls == []
        assert "tool_calls" not in msg.additional_kwargs
        assert "function_call" not in msg.additional_kwargs
        assert msg.response_metadata["finish_reason"] == "stop"


class TestToolFrequencyDetection:
    """Tests for per-tool-type frequency detection (Layer 2).

    This catches the case where an agent calls the same tool type many times
    with *different* arguments (e.g. read_file on 40 different files), which
    bypasses hash-based detection.
    """

    def _read_call(self, path):
        return {"name": "read_file", "id": f"call_read_{path}", "args": {"path": path}}

    def test_below_freq_warn_returns_none(self):
        mw = LoopDetectionMiddleware(tool_freq_warn=5, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        for i in range(4):
            result = mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)
            assert result is None

    def test_freq_warn_at_threshold(self):
        mw = LoopDetectionMiddleware(tool_freq_warn=5, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        for i in range(4):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        # 5th call queues a per-tool-type frequency warning; state untouched.
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_4.py")]), runtime)
        assert result is None
        queued = mw._pending_warnings.get(_pending_key(), [])
        assert queued
        assert "read_file" in queued[0]
        assert "LOOP DETECTED" in queued[0]

    def test_freq_warn_only_queued_once(self):
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        # 3rd queues a frequency warning.
        mw._apply(_make_state(tool_calls=[self._read_call("/file_2.py")]), runtime)
        assert len(mw._pending_warnings[_pending_key()]) == 1

        # 4th: same tool name, no additional enqueue.
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_3.py")]), runtime)
        assert result is None
        assert len(mw._pending_warnings[_pending_key()]) == 1

    def test_freq_hard_stop_at_limit(self):
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=6)
        runtime = _make_runtime()

        for i in range(5):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        # 6th call triggers hard stop
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_5.py")]), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg, AIMessage)
        assert msg.tool_calls == []
        assert "FORCED STOP" in msg.content
        assert "read_file" in msg.content

    def test_different_tools_tracked_independently(self):
        """read_file and bash should have independent frequency counters."""
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        # 2 read_file calls
        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        # 2 bash calls — should not trigger (bash count = 2, read_file count = 2)
        for i in range(2):
            result = mw._apply(_make_state(tool_calls=[_bash_call(f"cmd_{i}")]), runtime)
            assert result is None

        # 3rd read_file triggers — warning is queued (state unchanged).
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_2.py")]), runtime)
        assert result is None
        assert "read_file" in mw._pending_warnings[_pending_key()][0]

    def test_freq_reset_clears_state(self):
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        mw.reset()

        # After reset, count restarts — should not trigger
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_new.py")]), runtime)
        assert result is None

    def test_freq_reset_per_thread_clears_only_target(self):
        """reset(thread_id=...) should clear frequency state for that thread only."""
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=10)
        runtime_a = _make_runtime("thread-A")
        runtime_b = _make_runtime("thread-B")

        # 2 calls on each thread
        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/a_{i}.py")]), runtime_a)
            mw._apply(_make_state(tool_calls=[self._read_call(f"/b_{i}.py")]), runtime_b)

        # Reset only thread-A
        mw.reset(thread_id="thread-A")

        assert "thread-A" not in mw._tool_freq
        assert "thread-A" not in mw._tool_freq_warned

        # thread-B state should still be intact — 3rd call queues a warn.
        result = mw._apply(_make_state(tool_calls=[self._read_call("/b_2.py")]), runtime_b)
        assert result is None
        assert "LOOP DETECTED" in mw._pending_warnings[_pending_key("thread-B")][0]

        # thread-A restarted from 0 — should not trigger
        result = mw._apply(_make_state(tool_calls=[self._read_call("/a_new.py")]), runtime_a)
        assert result is None

    def test_freq_per_thread_isolation(self):
        """Frequency counts should be independent per thread."""
        mw = LoopDetectionMiddleware(tool_freq_warn=3, tool_freq_hard_limit=10)
        runtime_a = _make_runtime("thread-A")
        runtime_b = _make_runtime("thread-B")

        # 2 calls on thread A
        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime_a)

        # 2 calls on thread B — should NOT push thread A over threshold
        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/other_{i}.py")]), runtime_b)

        # 3rd call on thread A — queues a warning (count=3 for thread A only).
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_2.py")]), runtime_a)
        assert result is None
        assert "LOOP DETECTED" in mw._pending_warnings[_pending_key("thread-A")][0]
        assert not mw._pending_warnings.get(_pending_key("thread-B"))

    def test_multi_tool_single_response_counted(self):
        """When a single response has multiple tool calls, each is counted."""
        mw = LoopDetectionMiddleware(tool_freq_warn=5, tool_freq_hard_limit=10)
        runtime = _make_runtime()

        # Response 1: 2 read_file calls → count = 2
        call = [self._read_call("/a.py"), self._read_call("/b.py")]
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is None

        # Response 2: 2 more → count = 4
        call = [self._read_call("/c.py"), self._read_call("/d.py")]
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is None

        # Response 3: 1 more → count = 5 → queues warn.
        result = mw._apply(_make_state(tool_calls=[self._read_call("/e.py")]), runtime)
        assert result is None
        assert "read_file" in mw._pending_warnings[_pending_key()][0]

    def test_override_tool_uses_override_thresholds(self):
        """A tool in tool_freq_overrides uses its own thresholds, not the global ones."""
        mw = LoopDetectionMiddleware(
            tool_freq_warn=5,
            tool_freq_hard_limit=10,
            tool_freq_overrides={"bash": (50, 100)},
        )
        runtime = _make_runtime()

        # 10 bash calls — would hit global hard_limit=10, but bash override is 100
        for i in range(10):
            result = mw._apply(_make_state(tool_calls=[_bash_call(f"cmd_{i}")]), runtime)
            assert result is None, f"unexpected trigger on call {i + 1}"

    def test_non_override_tool_falls_back_to_global(self):
        """A tool NOT in tool_freq_overrides uses the global warn/hard_limit."""
        mw = LoopDetectionMiddleware(
            tool_freq_warn=3,
            tool_freq_hard_limit=6,
            tool_freq_overrides={"bash": (50, 100)},
        )
        runtime = _make_runtime()

        for i in range(2):
            mw._apply(_make_state(tool_calls=[self._read_call(f"/file_{i}.py")]), runtime)

        # 3rd read_file call hits global warn=3 (read_file has no override).
        # Warning delivery is deferred to wrap_model_call so the just-emitted
        # AIMessage(tool_calls=...) is not mutated before ToolMessages exist.
        result = mw._apply(_make_state(tool_calls=[self._read_call("/file_2.py")]), runtime)
        assert result is None
        queued = mw._pending_warnings.get(_pending_key(), [])
        assert queued
        assert "read_file" in queued[0]

    def test_hash_detection_takes_priority(self):
        """Hash-based hard stop fires before frequency check for identical calls."""
        mw = LoopDetectionMiddleware(
            warn_threshold=2,
            hard_limit=3,
            tool_freq_warn=100,
            tool_freq_hard_limit=200,
        )
        runtime = _make_runtime()
        call = [self._read_call("/same_file.py")]

        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)

        # 3rd identical call → hash hard_limit=3 fires (not freq)
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg, AIMessage)
        assert _HARD_STOP_MSG in msg.content


class TestFromConfig:
    """Tests for LoopDetectionMiddleware.from_config — the sole validated construction path."""

    @staticmethod
    def _config(**kwargs):
        from deerflow.config.loop_detection_config import LoopDetectionConfig

        return LoopDetectionConfig(**kwargs)

    def test_scalar_fields_mapped(self):
        config = self._config(
            warn_threshold=4,
            hard_limit=8,
            window_size=15,
            max_tracked_threads=50,
            tool_freq_warn=20,
            tool_freq_hard_limit=40,
        )
        mw = LoopDetectionMiddleware.from_config(config)
        assert mw.warn_threshold == 4
        assert mw.hard_limit == 8
        assert mw.window_size == 15
        assert mw.max_tracked_threads == 50
        assert mw.tool_freq_warn == 20
        assert mw.tool_freq_hard_limit == 40

    def test_overrides_converted_to_tuples(self):
        config = self._config(tool_freq_overrides={"bash": {"warn": 50, "hard_limit": 100}})
        mw = LoopDetectionMiddleware.from_config(config)
        assert mw._tool_freq_overrides == {"bash": (50, 100)}

    def test_empty_overrides(self):
        mw = LoopDetectionMiddleware.from_config(self._config())
        assert mw._tool_freq_overrides == {}

    def test_constructed_middleware_queues_loop_warning(self):
        mw = LoopDetectionMiddleware.from_config(self._config(warn_threshold=2, hard_limit=4))
        runtime = _make_runtime()
        call = [_bash_call("ls")]
        mw._apply(_make_state(tool_calls=call), runtime)
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is None
        queued = mw._pending_warnings.get(_pending_key(), [])
        assert queued
        assert "LOOP DETECTED" in queued[0]


class TestSubagentScopeAndCumulativeCounting:
    """子代理接入 loop detection（Phase 1 / Task 3）。

    两个缺陷必须一起修，否则接入等于无效或有害：

    1. **滑窗漏检**：判定子代理的 49 次 bash 里夹杂大量 read/grep，两次同参
       `uncertain_recheck.py` 的间隔常常超过 `window_size=20`，哈希滑出窗口后
       `history.count()` 永远到不了阈值 —— 12 次同参重跑就是这样没人拦。
    2. **跨 task 串味**：跟踪键原本只有 `thread_id`，一个 run 内 14 个 task 会把彼此
       的调用计入同一个计数器，A 的重复会让 B 被硬停。
    """

    GATE = "python3 /mnt/skills/custom/eligibility-judgment/scripts/uncertain_recheck.py --criteria a.json"

    def test_cumulative_counting_is_off_by_default(self):
        """默认关闭：lead agent 的滑窗语义（`test_window_sliding`）不能被改掉。"""
        assert LoopDetectionMiddleware().cumulative_counting is False
        assert LoopDetectionConfig().cumulative_counting is False

    def test_cumulative_count_survives_window_eviction(self):
        """同参调用间隔 > window_size 时仍必须累计触发（告警走 pending 队列，不改 state）。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, window_size=2, cumulative_counting=True)
        runtime = _make_runtime()
        call = [_bash_call("grep -n foo bar.md")]

        mw._apply(_make_state(tool_calls=call), runtime)
        for i in range(5):  # 把它挤出滑窗
            mw._apply(_make_state(tool_calls=[_bash_call(f"other_{i}")]), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)
        for i in range(5):
            mw._apply(_make_state(tool_calls=[_bash_call(f"more_{i}")]), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)

        queued = mw._pending_warnings.get(_pending_key(), [])
        assert queued, "第 3 次同参调用必须告警，哪怕早已滑出窗口"
        assert "LOOP DETECTED" in queued[0]

    def test_cumulative_count_reaches_hard_stop_across_window(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, window_size=2, cumulative_counting=True)
        runtime = _make_runtime()
        call = [_bash_call("grep -n foo bar.md")]
        results = []
        for i in range(5):
            results.append(mw._apply(_make_state(tool_calls=call), runtime))
            mw._apply(_make_state(tool_calls=[_bash_call(f"filler_{i}")]), runtime)

        hard = [r for r in results if r is not None]
        assert hard, f"5 次同参调用必须硬停，即使中间夹了别的调用：{results}"
        msgs = hard[-1]["messages"]
        assert msgs[0].tool_calls == [], "硬停必须剥离 tool_calls"
        assert _HARD_STOP_MSG in msgs[0].content

    def test_window_sliding_still_resets_when_cumulative_off(self):
        """回归：关闭累计时行为与改动前一致。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, window_size=5)
        runtime = _make_runtime()
        call = [_bash_call("ls")]
        mw._apply(_make_state(tool_calls=call), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)
        for i in range(5):
            mw._apply(_make_state(tool_calls=[_bash_call(f"other_{i}")]), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)
        assert not mw._pending_warnings.get(_pending_key())

    def test_tasks_of_one_run_do_not_share_counters(self):
        """告警队列按 (thread, run) 归集，所以这里断言计数器本身而非队列。"""
        mw = LoopDetectionMiddleware(warn_threshold=2, cumulative_counting=True)
        call = [_bash_call("ls")]
        task_a = _make_runtime(task_id="task-a")
        task_b = _make_runtime(task_id="task-b")

        mw._apply(_make_state(tool_calls=call), task_a)
        mw._apply(_make_state(tool_calls=call), task_a)
        mw._apply(_make_state(tool_calls=call), task_b)

        scope_a = mw._get_tracking_scope(task_a)
        scope_b = mw._get_tracking_scope(task_b)
        assert scope_a != scope_b
        assert max(mw._cumulative[scope_a].values()) == 2
        assert max(mw._cumulative[scope_b].values()) == 1, "另一个 task 不得继承计数"

    def test_lead_scope_unchanged_without_task_id(self):
        """无 task_id 时跟踪键仍是 thread_id —— 既有断言（`mw._history["test-thread"]`）不变。"""
        mw = LoopDetectionMiddleware(warn_threshold=5)
        mw._apply(_make_state(tool_calls=[_bash_call("ls")]), _make_runtime())
        assert "test-thread" in mw._history

    def test_subagent_scope_key_includes_run_and_task(self):
        mw = LoopDetectionMiddleware(warn_threshold=5)
        mw._apply(_make_state(tool_calls=[_bash_call("ls")]), _make_runtime(task_id="task-a"))
        assert "test-thread" not in mw._history
        assert any("task-a" in scope for scope in mw._history), f"scope 必须含 task_id：{list(mw._history)}"

    def test_gate_script_repeats_still_count_normally(self):
        """⛔ verification 豁免已删除（2026-08-19）：门禁/哈希这类幂等命令重跑不再有
        独立预算，与普通调用同阈值计数。合法的修复->验证循环需要更高额度时，应调
        ``tool_freq_overrides.bash`` 或后续在 ``_stable_tool_key`` 层修复，而不是靠
        文件名白名单。这里钉住回归行为：同一条门禁命令第 3 次照常告警。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, cumulative_counting=True)
        runtime = _make_runtime(task_id="task-a")
        call = [_bash_call(self.GATE)]
        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)
        assert not mw._pending_warnings.get(_pending_key())
        mw._apply(_make_state(tool_calls=call), runtime)  # 第 3 次
        assert mw._pending_warnings.get(_pending_key()), "无豁免后，第 3 次同参门禁命令必须告警"

    def test_cumulative_state_is_bounded(self):
        mw = LoopDetectionMiddleware(warn_threshold=100, hard_limit=100, cumulative_counting=True)
        runtime = _make_runtime(task_id="task-a")
        for i in range(_MAX_CUMULATIVE_HASHES + 50):
            mw._apply(_make_state(tool_calls=[_bash_call(f"cmd_{i}")]), runtime)
        for counts in mw._cumulative.values():
            assert len(counts) <= _MAX_CUMULATIVE_HASHES

    def test_reset_clears_cumulative_state(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, cumulative_counting=True)
        runtime = _make_runtime(task_id="task-a")
        call = [_bash_call("ls")]
        mw._apply(_make_state(tool_calls=call), runtime)
        mw.reset()
        assert not mw._cumulative
        mw._apply(_make_state(tool_calls=call), runtime)
        assert not mw._pending_warnings.get(_pending_key())

    def test_from_config_carries_cumulative_flag(self):
        config = LoopDetectionConfig(cumulative_counting=True)
        assert LoopDetectionMiddleware.from_config(config).cumulative_counting is True
        # 显式覆盖：子代理链按 subagents.loop_detection 开启累计，而 lead 用全局默认
        assert LoopDetectionMiddleware.from_config(LoopDetectionConfig(), cumulative_counting=True).cumulative_counting is True


class TestBashCommandMutates:
    """写形态判定:通用 Unix 知识,不含任何业务 skill 文件名。"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            # 观测到的复检命令 -- 只读
            ("sha256sum /w/j.json | cut -c1-12", False),
            ("python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py --track EX", False),
            ("grep -n foo bar.md", False),
            ("cat foo | grep x", False),
            ("ls -la /w", False),
            # 描述符复制不是文件写
            ("grep foo bar 2>&1", False),
            ("cmd > /dev/null 2>&1", False),
            # 写形态:重定向
            ("echo hi > /tmp/f", True),
            ("echo hi >> /tmp/f", True),
            ("python3 x.py > /tmp/out.log", True),
            # 写形态:变更原语(token 级匹配,子串不算)
            ("rm -rf /tmp/x", True),
            ("mv a b", True),
            ("cp a b", True),
            ("mkdir d", True),
            ("tee /tmp/f", True),
            ("chmod +x x.sh", True),
            # sed 只有 -i 才写
            ("sed s/a/b/ f", False),
            ("sed -i s/a/b/ f", True),
            # 子串陷阱:permit/firms 不是 rm
            ("permit user", False),
            ("firms list", False),
        ],
    )
    def test_classification(self, command, expected):
        from deerflow.agents.middlewares.loop_detection_middleware import _bash_command_mutates

        assert _bash_command_mutates(command) is expected


def _patch_call(i: int) -> dict:
    """第 i 批 apply_json_patches -- 每批不同的 patch(内容敏感哈希,彼此不同)。"""
    return {
        "name": "apply_json_patches",
        "id": f"call_p{i}",
        "args": {
            "path": "/w/j.json",
            "expected_hash": f"h{i}",
            "patches": [{"pointer": f"/items/{i}/conclusion", "op": "replace", "value": "v"}],
        },
    }


def _read_call(path="/w/a.py", start=None, end=None) -> dict:
    args = {"path": path}
    if start is not None:
        args["start_line"] = start
    if end is not None:
        args["end_line"] = end
    return {"name": "read_file", "id": "call_read", "args": args}


class TestMutationAwareReset:
    """mutation_reset:改判/修订循环里「改动后的复检」不是死循环证据。

    会话 98d27624 的 8 次误杀全部是同一形状:重复调用之间夹着 apply_json_patches
    (sha256sum -> patch -> 闸 -> sha256sum 是 criteria-repair.md 的规定节奏)。
    重置语义只认「别的调用动过世界」,自己造成的 bump 不算 -- 否则 rm -rf x5
    这种变更命令死循环会被自己的 bump 洗掉。
    """

    DIGEST = "sha256sum /mnt/user-data/workspace/criteria_parsed_EX.json | cut -c1-12"
    GATE = "python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py --track EX"

    def test_digest_recheck_after_each_patch_does_not_warn(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime(task_id="t")
        for i in range(4):
            assert mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime) is None
            assert mw._apply(_make_state(tool_calls=[_patch_call(i)]), runtime) is None
        assert not mw._pending_warnings.get(_pending_key())

    def test_gate_rerun_after_each_patch_does_not_warn(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime(task_id="t")
        for i in range(4):
            assert mw._apply(_make_state(tool_calls=[_bash_call(self.GATE)]), runtime) is None
            assert mw._apply(_make_state(tool_calls=[_patch_call(i)]), runtime) is None
        assert not mw._pending_warnings.get(_pending_key())

    def test_window_mode_reset(self):
        """滑窗模式下重置走「清窗口历史」路径。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, mutation_reset=True)
        runtime = _make_runtime()
        for i in range(4):
            assert mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime) is None
            assert mw._apply(_make_state(tool_calls=[_patch_call(i)]), runtime) is None
        assert not mw._pending_warnings.get(_pending_key())

    def test_readonly_repeat_without_mutation_still_warns(self):
        """无变更的相同只读命令照常告警 -- P0 语义不变。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        call = [_bash_call(self.DIGEST)]
        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)
        assert not mw._pending_warnings.get(_pending_key())
        mw._apply(_make_state(tool_calls=call), runtime)  # 第 3 次
        assert mw._pending_warnings.get(_pending_key())

    def test_unknown_command_repeat_still_warns(self):
        """python3 脚本属「不可分类」-> 不判为写 -> 重跑照常计数(保守方向保检测力)。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        call = [_bash_call(self.GATE)]
        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)
        assert mw._pending_warnings.get(_pending_key())

    def test_alternating_readonly_verify_pair_still_warns(self):
        """sha; 闸; sha; 闸; sha -- 两者都只读、互不 bump,必须告警。

        这是 bash 不能整体判为写形态的原因:否则一条「验证对」死循环会被互相洗掉。
        """
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        for _ in range(2):
            mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime)
            mw._apply(_make_state(tool_calls=[_bash_call(self.GATE)]), runtime)
        mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime)
        assert mw._pending_warnings.get(_pending_key())

    def test_mutating_loop_still_hard_stops(self):
        """自排除:rm 自己的 bump 不得重置自己的计数器。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        call = [_bash_call("rm -rf /tmp/x")]
        result = None
        for _ in range(4):
            result = mw._apply(_make_state(tool_calls=call), runtime)
        assert mw._pending_warnings.get(_pending_key())  # 第 3 次已告警
        result = mw._apply(_make_state(tool_calls=call), runtime)  # 第 5 次
        assert result is not None
        assert result["messages"][0].tool_calls == []

    def test_redirect_writer_loop_still_hard_stops(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        call = [_bash_call("echo hi > /tmp/f")]
        for _ in range(4):
            mw._apply(_make_state(tool_calls=call), runtime)
        result = mw._apply(_make_state(tool_calls=call), runtime)
        assert result is not None
        assert result["messages"][0].tool_calls == []

    def test_identical_write_loop_still_warns(self):
        """read; write(同内容); read; write... -- 读被重置,但写哈希不变照常累计。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        write = [{"name": "write_file", "id": "call_w", "args": {"path": "/w/a.py", "content": "same"}}]
        for _ in range(2):
            mw._apply(_make_state(tool_calls=[_read_call()]), runtime)
            mw._apply(_make_state(tool_calls=write), runtime)
        assert not mw._pending_warnings.get(_pending_key())
        mw._apply(_make_state(tool_calls=[_read_call()]), runtime)
        mw._apply(_make_state(tool_calls=write), runtime)  # 第 3 次同参写
        assert mw._pending_warnings.get(_pending_key())

    def test_reset_budget_bounds_alternating_writers(self):
        """两个写命令互相 bump(A写; B写; A写; ...) -- 预算耗尽后必须恢复检测。"""
        mw = LoopDetectionMiddleware(
            warn_threshold=3,
            hard_limit=5,
            cumulative_counting=True,
            mutation_reset=True,
            mutation_reset_budget=2,
        )
        runtime = _make_runtime()
        a = [_bash_call("echo a > /tmp/a")]
        b = [_bash_call("echo b > /tmp/b")]
        # A 出现序列:occ1=1, occ2/occ3 重置, occ4=2, occ5=3 -> 告警
        for _ in range(4):
            mw._apply(_make_state(tool_calls=a), runtime)
            mw._apply(_make_state(tool_calls=b), runtime)
        assert not mw._pending_warnings.get(_pending_key())
        mw._apply(_make_state(tool_calls=a), runtime)  # 第 5 次
        assert mw._pending_warnings.get(_pending_key())

    def test_reset_clears_warned_so_new_loop_can_warn_again(self, caplog):
        """告警过 -> 改动 -> 又开始真循环:必须能再次告警,而不是被 _warned 记忆吞掉。"""
        import logging

        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime()
        call = [_bash_call(self.DIGEST)]
        with caplog.at_level(logging.WARNING, logger="deerflow.agents.middlewares.loop_detection_middleware"):
            for _ in range(3):  # 真循环 -> 第 3 次告警
                mw._apply(_make_state(tool_calls=call), runtime)
            assert len([r for r in caplog.records if "Repetitive tool calls" in r.message]) == 1

            mw._apply(_make_state(tool_calls=[_patch_call(0)]), runtime)  # 世界变了
            for _ in range(2):  # 只有第一次复检免费:count 1 -> 2,不告警
                mw._apply(_make_state(tool_calls=call), runtime)
            assert len([r for r in caplog.records if "Repetitive tool calls" in r.message]) == 1

            mw._apply(_make_state(tool_calls=[_patch_call(1)]), runtime)
            for _ in range(2):
                mw._apply(_make_state(tool_calls=call), runtime)
            mw._apply(_make_state(tool_calls=call), runtime)  # 重置后的第 3 次
            assert len([r for r in caplog.records if "Repetitive tool calls" in r.message]) == 2

    def test_off_by_default(self):
        """默认关闭:lead agent 行为与历史一致(改动间有写也照样计数)。"""
        assert LoopDetectionMiddleware().mutation_reset is False
        assert LoopDetectionConfig().mutation_reset is False

        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, cumulative_counting=True)
        runtime = _make_runtime()
        for i in range(3):
            mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime)
            mw._apply(_make_state(tool_calls=[_patch_call(i)]), runtime)
        assert mw._pending_warnings.get(_pending_key()), "关闭重置时,第 3 次相同命令必须告警"

    def test_from_config_override(self):
        assert LoopDetectionMiddleware.from_config(LoopDetectionConfig(), mutation_reset=True).mutation_reset is True
        assert LoopDetectionMiddleware.from_config(LoopDetectionConfig(mutation_reset=True)).mutation_reset is True
        # 预算取全局配置
        mw = LoopDetectionMiddleware.from_config(LoopDetectionConfig(mutation_reset_budget=3))
        assert mw.mutation_reset_budget == 3

    def test_reset_clears_mutation_state(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, cumulative_counting=True, mutation_reset=True)
        runtime = _make_runtime(task_id="t")
        mw._apply(_make_state(tool_calls=[_bash_call(self.DIGEST)]), runtime)
        mw._apply(_make_state(tool_calls=[_patch_call(0)]), runtime)
        assert mw._mut_state and mw._mutation_epoch
        mw.reset()
        assert not mw._mut_state and not mw._mutation_epoch


class TestReadFileExactRangeKey:
    """read_file 键从 200 行分桶改为精确区间。

    会话 98d27624 里 2 次误杀是分桶折叠:[90-115]/[1-120]/[1-140] 三个不同意图
    的分页读落进同一个桶。分桶原本容忍的「文件改了之后的近似重读」已由
    mutation_reset 接管(写过就重置),桶只剩纯害。
    """

    def test_distinct_ranges_are_distinct_keys(self):
        a = _hash_tool_calls([_read_call(start=90, end=115)])
        b = _hash_tool_calls([_read_call(start=1, end=120)])
        c = _hash_tool_calls([_read_call(start=1, end=140)])
        assert len({a, b, c}) == 3

    def test_same_range_same_key(self):
        a = _hash_tool_calls([_read_call(start=1, end=220)])
        b = _hash_tool_calls([_read_call(start=1, end=220)])
        assert a == b

    def test_drifting_range_is_distinct_but_layer2_backstops(self):
        """[1-100] -> [1-101] 漂移读逃过 Layer 1 是设计使然,由频次层兜底。"""
        a = _hash_tool_calls([_read_call(start=1, end=100)])
        b = _hash_tool_calls([_read_call(start=1, end=101)])
        assert a != b

    def test_distinct_ranges_do_not_warn_without_mutation(self):
        """无变更区间里三次不同分页读不再折叠告警(EU0OLUHa 形态)。"""
        mw = LoopDetectionMiddleware(warn_threshold=3, cumulative_counting=True)
        runtime = _make_runtime()
        for start, end in ((90, 115), (1, 120), (1, 140)):
            assert mw._apply(_make_state(tool_calls=[_read_call(start=start, end=end)]), runtime) is None
        assert not mw._pending_warnings.get(_pending_key())

    def test_identical_range_repeat_still_warns(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, cumulative_counting=True)
        runtime = _make_runtime()
        call = [_read_call(start=1, end=100)]
        for _ in range(2):
            mw._apply(_make_state(tool_calls=call), runtime)
        mw._apply(_make_state(tool_calls=call), runtime)
        assert mw._pending_warnings.get(_pending_key())
