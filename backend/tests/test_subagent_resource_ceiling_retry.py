"""资源上限失败不得盲目重试（Phase 1 / Task 5、6）。

**故障**：会话 `d393714d` 里 IN 轨判定 task 烧到 6.36M token 后 FAILED，`task_tool` 立刻
无条件重试，第二次又跑 72 步 / 5.21M —— 失败代价直接翻倍到 11.57M，而两次撞的是同一个
天花板（`recursion_limit = max_turns`）。

`SUBAGENT_MAX_RETRIES` 的注释里其实早就写明了正确的判据：「超时永不重试，因为超时说明这
份工作对预算来说太大了，重跑只会把同样的预算再烧一遍」。递归/预算耗尽与超时**同类**，
所以本文件锁定：带 `stop_reason ∈ RESOURCE_CEILING_STOP_REASONS` 的失败默认不重试，
且该原因必须出现在回报文本与 `subagent.end` 事件里，让 lead 能据实决定定向补跑。
"""

from __future__ import annotations

import pytest

from deerflow.config.subagents_config import SubagentsAppConfig


@pytest.fixture
def task_tool_module():
    import importlib

    return importlib.import_module("deerflow.tools.builtins.task_tool")


class _Config:
    """最小 app_config 替身：只需要 subagents.graceful_stop。"""

    def __init__(self, *, retry_resource_ceiling_failures: bool = False):
        self.subagents = SubagentsAppConfig()
        self.subagents.graceful_stop.retry_resource_ceiling_failures = retry_resource_ceiling_failures


class TestIsRetryableFailure:
    def test_ordinary_failure_is_retryable(self, task_tool_module):
        assert task_tool_module._is_retryable_failure(None, _Config()) is True

    def test_transient_looking_reason_is_retryable(self, task_tool_module):
        assert task_tool_module._is_retryable_failure("provider_5xx", _Config()) is True

    @pytest.mark.parametrize("reason", ["recursion_limit", "token_budget"])
    def test_resource_ceiling_is_not_retried_by_default(self, task_tool_module, reason):
        assert task_tool_module._is_retryable_failure(reason, _Config()) is False

    @pytest.mark.parametrize("reason", ["recursion_limit", "token_budget"])
    def test_resource_ceiling_retry_can_be_opted_back_in(self, task_tool_module, reason):
        config = _Config(retry_resource_ceiling_failures=True)
        assert task_tool_module._is_retryable_failure(reason, config) is True

    def test_missing_config_falls_back_to_not_retrying(self, task_tool_module, monkeypatch):
        """拿不到配置时选择「不重试」——重试的代价是再烧一遍同样的预算。"""

        def _raise():
            raise FileNotFoundError("config.yaml")

        monkeypatch.setattr(task_tool_module, "get_app_config", _raise, raising=False)
        assert task_tool_module._is_retryable_failure("recursion_limit", None) is False


class TestClassifyStopReason:
    """异常 → stop_reason 的判定（`GraphRecursionError` 按类名匹配，不硬导入）。"""

    @pytest.fixture
    def classify(self):
        from deerflow.subagents.stop_reasons import classify_stop_reason

        return classify_stop_reason

    def test_graph_recursion_error_by_class_name(self, classify):
        cls = type("GraphRecursionError", (RuntimeError,), {})
        assert classify(cls("boom")) == "recursion_limit"

    def test_recursion_limit_message(self, classify):
        exc = RuntimeError("Recursion limit of 150 reached without hitting a stop condition")
        assert classify(exc) == "recursion_limit"

    def test_wrapped_cause_is_inspected(self, classify):
        cls = type("GraphRecursionError", (RuntimeError,), {})
        outer = RuntimeError("subagent failed")
        outer.__cause__ = cls("Recursion limit of 150 reached")
        assert classify(outer) == "recursion_limit"

    def test_ordinary_error_has_no_stop_reason(self, classify):
        assert classify(ValueError("bad json")) is None

    def test_self_referencing_cause_does_not_hang(self, classify):
        exc = ValueError("loop")
        exc.__cause__ = exc
        assert classify(exc) is None


class TestStopReasonIsPersisted:
    """`subagent.end` 必须带上停止原因，否则离线报告分不清「没额度了」和「坏了」。"""

    def test_terminal_event_carries_stop_reason(self):
        from deerflow.subagents.step_events import subagent_run_event

        event = subagent_run_event(
            {
                "type": "task_failed",
                "task_id": "task-1",
                "error": "Recursion limit of 150 reached",
                "stop_reason": "recursion_limit",
            }
        )
        assert event is not None
        assert event["event_type"] == "subagent.end"
        assert event["metadata"]["stop_reason"] == "recursion_limit"
        assert event["content"]["stop_reason"] == "recursion_limit"

    def test_absent_stop_reason_stays_absent(self):
        from deerflow.subagents.step_events import subagent_run_event

        event = subagent_run_event({"type": "task_completed", "task_id": "task-1", "result": "done"})
        assert event is not None
        assert "stop_reason" not in event["metadata"]
        assert "stop_reason" not in event["content"]


class TestResourceCeilingConstants:
    def test_recursion_and_budget_are_the_two_ceilings(self):
        from deerflow.subagents.stop_reasons import RESOURCE_CEILING_STOP_REASONS, is_resource_ceiling

        assert RESOURCE_CEILING_STOP_REASONS == frozenset({"recursion_limit", "token_budget"})
        assert is_resource_ceiling("recursion_limit") is True
        assert is_resource_ceiling(None) is False

    def test_executor_reexports_the_same_set(self):
        """执行器仍可导出同一份定义，避免出现第二份「什么算资源上限」的口径。"""
        import importlib

        stop_reasons = importlib.import_module("deerflow.subagents.stop_reasons")
        assert stop_reasons.RESOURCE_CEILING_STOP_REASONS == frozenset({"recursion_limit", "token_budget"})


# --------------------------------------------------------------------------- #
# 重试必须告诉子代理上次错在哪（thread 7512ebd2）                                #
# --------------------------------------------------------------------------- #
#
# 重试原先用**逐字相同**的 prompt 再跑一遍，而子代理上下文是隔离的，没有任何别的通道
# 能知道上一次为什么被拒。7512ebd2：两轨判定都因自创文件名（`judgment_IN.md` /
# `judgments_EX.json`）撞产物闸，重试收到同一份 prompt、做了同一次替换、撞同一道闸。
# 四次尝试、约 7.5M token、零产物。


class TestRetryPromptCarriesTheFailureReason:
    GATE_ERROR = "Subagent reported completion but required outputs missing/empty. missing=['/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_IN.json'] Write each declared path with write_file before finishing."

    def test_original_assignment_is_preserved_verbatim(self, task_tool_module):
        prompt = "请按 /eligibility-judgment 技能规则，对患者 MCRC-2150006 的**入选标准**逐条判定。"

        out = task_tool_module._retry_prompt(prompt, self.GATE_ERROR)

        assert out.startswith(prompt), "任务书是 lead 的契约，重试不得改写它"

    def test_failure_reason_is_appended(self, task_tool_module):
        out = task_tool_module._retry_prompt("原任务", self.GATE_ERROR)

        assert "judgments_draft_MCRC-2150006_IN.json" in out
        assert "RETRY" in out

    @pytest.mark.parametrize("error", [None, "", "   ", "\n"])
    def test_no_reason_leaves_the_prompt_untouched(self, task_tool_module, error):
        assert task_tool_module._retry_prompt("原任务", error) == "原任务"

    def test_long_reason_is_capped(self, task_tool_module):
        out = task_tool_module._retry_prompt("原任务", "E" * 20000)

        assert len(out) < 4000, "堆栈或整份 payload 会把真正的任务挤出注意力"
        assert "truncated" in out
