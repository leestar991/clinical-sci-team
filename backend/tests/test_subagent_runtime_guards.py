"""子代理运行时三道防线（Phase 1 / Task 3、4、5）。

**根因**：`build_subagent_runtime_middlewares` 组装的链里没有 LoopDetection、没有
Summarization、也没有 TokenBudget —— 三者 lead agent 从一开始就有。后果是一个判定子代理
可以在 83 步里把上下文单调堆到 6.36M token：没人压缩它的上下文、没人给它设上限、也没人
打断它第 12 次跑同一条 `uncertain_recheck.py`。

**本文件锁定两件事**：
1. 三者都**默认关闭**，关闭时链形与改动前逐项一致（未改配置的部署行为不变）；
2. 打开时确实出现在子代理链中，且顺序满足 `SafetyFinishReasonMiddleware` 的约定
   （它必须排在 LoopDetection 之后）。
"""

from __future__ import annotations

import pytest

from deerflow.agents.middlewares.tool_error_handling_middleware import (
    build_subagent_runtime_middlewares,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.loop_detection_config import LoopDetectionConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.summarization_config import ContextSize, SummarizationConfig
from deerflow.config.token_budget_config import TokenBudgetConfig


def _app_config(**over) -> AppConfig:
    config = AppConfig(
        models=[ModelConfig(name="m1", use="langchain_openai:ChatOpenAI", model="gpt-4o", api_key="k")],
        sandbox=SandboxConfig(use="deerflow.sandbox.local_sandbox:LocalSandboxProvider"),
    )
    for key, value in over.items():
        setattr(config, key, value)
    return config


def _names(middlewares) -> list[str]:
    return [type(m).__name__ for m in middlewares]


def _triggers(middleware) -> list[tuple]:
    """归一化触发条件：单条与多条两种写法都要能断言。"""
    trigger = getattr(middleware, "trigger", None)
    if trigger is None:
        return []
    return list(trigger) if isinstance(trigger, list) else [trigger]


def _keep(middleware) -> tuple:
    return getattr(middleware, "keep", None)


@pytest.fixture
def base_config() -> AppConfig:
    return _app_config()


class TestDefaultsAreOff:
    def test_all_three_guards_default_to_disabled(self, base_config):
        subagents = base_config.subagents
        assert subagents.loop_detection.enabled is False
        assert subagents.summarization.enabled is False
        assert subagents.token_budget.enabled is False

    def test_chain_shape_unchanged_when_disabled(self, base_config):
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        assert "LoopDetectionMiddleware" not in names
        assert "DeerFlowSummarizationMiddleware" not in names
        assert "TokenBudgetMiddleware" not in names

    def test_cumulative_counting_defaults_on_for_subagents(self, base_config):
        """子代理的重复调用间隔比窗口宽，所以一旦开启就该用累计计数。"""
        assert base_config.subagents.loop_detection.cumulative_counting is True

    def test_resource_ceiling_retry_defaults_off(self, base_config):
        """预算/递归耗尽后重试只会把同样的上限再烧一遍（超时早就是这么处理的）。"""
        assert base_config.subagents.graceful_stop.retry_resource_ceiling_failures is False


class TestLoopDetectionWiring:
    def test_attached_when_enabled(self, base_config):
        base_config.subagents.loop_detection.enabled = True
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        assert "LoopDetectionMiddleware" in names

    def test_safety_finish_reason_stays_after_loop_detection(self, base_config):
        base_config.subagents.loop_detection.enabled = True
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        if "SafetyFinishReasonMiddleware" in names:
            assert names.index("LoopDetectionMiddleware") < names.index("SafetyFinishReasonMiddleware")

    def test_global_switch_still_wins(self, base_config):
        """全局 loop_detection 关掉时，子代理开关不得偷偷把它打开。"""
        base_config.loop_detection = LoopDetectionConfig(enabled=False)
        base_config.subagents.loop_detection.enabled = True
        assert "LoopDetectionMiddleware" not in _names(build_subagent_runtime_middlewares(app_config=base_config))

    def test_thresholds_come_from_global_section_but_counting_from_subagent(self, base_config):
        base_config.loop_detection = LoopDetectionConfig(warn_threshold=4, hard_limit=9, window_size=7)
        base_config.subagents.loop_detection.enabled = True
        mws = build_subagent_runtime_middlewares(app_config=base_config)
        loop = next(m for m in mws if type(m).__name__ == "LoopDetectionMiddleware")
        assert (loop.warn_threshold, loop.hard_limit, loop.window_size) == (4, 9, 7)
        assert loop.cumulative_counting is True

    def test_cumulative_counting_can_be_turned_off(self, base_config):
        base_config.subagents.loop_detection.enabled = True
        base_config.subagents.loop_detection.cumulative_counting = False
        mws = build_subagent_runtime_middlewares(app_config=base_config)
        loop = next(m for m in mws if type(m).__name__ == "LoopDetectionMiddleware")
        assert loop.cumulative_counting is False


class TestSummarizationWiring:
    def test_attached_when_enabled_with_independent_thresholds(self, base_config, monkeypatch):
        from unittest.mock import MagicMock

        import deerflow.models as models_module

        fake_model = MagicMock()
        fake_model.with_config.return_value = fake_model
        monkeypatch.setattr(models_module, "create_chat_model", lambda **kwargs: fake_model)

        base_config.summarization = SummarizationConfig(enabled=False)  # lead 关闭
        base_config.subagents.summarization = SummarizationConfig(
            enabled=True,
            trigger=ContextSize(type="tokens", value=120000),
            keep=ContextSize(type="tokens", value=60000),
        )
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        assert "DeerFlowSummarizationMiddleware" in names, "子代理压缩不应受 lead 开关影响"

    def test_lead_switch_does_not_enable_subagent_summarization(self, base_config, monkeypatch):
        from unittest.mock import MagicMock

        import deerflow.models as models_module

        fake_model = MagicMock()
        fake_model.with_config.return_value = fake_model
        monkeypatch.setattr(models_module, "create_chat_model", lambda **kwargs: fake_model)

        base_config.summarization = SummarizationConfig(enabled=True)
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        assert "DeerFlowSummarizationMiddleware" not in names

    def test_subagent_thresholds_reach_the_middleware(self, base_config, monkeypatch):
        """阈值必须真的传下去 —— 开关生效但阈值没生效，表现是"开了没用"。

        会话 `93d8a2c6` 就是被这种"看起来开了"骗过一轮：`read_file_dedup` 确实接上了，
        但它能命中的形态在该 workload 里只有 6 次。所以断言到构造出的中间件实例上，
        而不是只断言类名出现。
        """
        from unittest.mock import MagicMock

        import deerflow.models as models_module

        fake_model = MagicMock()
        fake_model.with_config.return_value = fake_model
        monkeypatch.setattr(models_module, "create_chat_model", lambda **kwargs: fake_model)

        base_config.subagents.summarization = SummarizationConfig(
            enabled=True,
            trigger=ContextSize(type="tokens", value=80000),
            keep=ContextSize(type="tokens", value=40000),
            trim_tokens_to_summarize=120000,
            summary_prompt="任务交接单 {messages}",
        )
        mws = build_subagent_runtime_middlewares(app_config=base_config)
        mw = next(m for m in mws if type(m).__name__ == "DeerFlowSummarizationMiddleware")
        assert ("tokens", 80000) in _triggers(mw)
        assert _keep(mw) == ("tokens", 40000)

    def test_summarization_runs_before_the_safety_guard(self, base_config, monkeypatch):
        """压缩必须在 SafetyFinishReason 之前 —— 后者要看的是最终要发出去的那批消息。"""
        from unittest.mock import MagicMock

        import deerflow.models as models_module

        fake_model = MagicMock()
        fake_model.with_config.return_value = fake_model
        monkeypatch.setattr(models_module, "create_chat_model", lambda **kwargs: fake_model)

        base_config.subagents.summarization = SummarizationConfig(enabled=True, trigger=ContextSize(type="tokens", value=80000))
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        if "SafetyFinishReasonMiddleware" in names:
            assert names.index("DeerFlowSummarizationMiddleware") < names.index("SafetyFinishReasonMiddleware")


class TestTokenBudgetWiring:
    def test_attached_when_enabled(self, base_config):
        base_config.subagents.token_budget = TokenBudgetConfig(enabled=True, max_tokens=1_500_000)
        mws = build_subagent_runtime_middlewares(app_config=base_config)
        budget = next(m for m in mws if type(m).__name__ == "TokenBudgetMiddleware")
        assert budget._config.max_tokens == 1_500_000

    def test_lead_budget_config_is_not_reused(self, base_config):
        """lead 的 token_budget 是「整个 run 的累计」，不能当作单 task 上限。"""
        base_config.token_budget = TokenBudgetConfig(enabled=True, max_tokens=10_000_000)
        names = _names(build_subagent_runtime_middlewares(app_config=base_config))
        assert "TokenBudgetMiddleware" not in names


class TestBudgetScopeIsPerTask:
    """预算按 task 归集：一个 run 里 14 个 task 不能共用一份额度。"""

    def _runtime(self, *, run_id="run-1", task_id=None):
        from types import SimpleNamespace

        context = {"run_id": run_id}
        if task_id is not None:
            context["task_id"] = task_id
        return SimpleNamespace(context=context)

    def test_task_scopes_are_distinct(self):
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        mw = TokenBudgetMiddleware.from_config(TokenBudgetConfig(enabled=True))
        a = mw._get_run_id(self._runtime(task_id="task-a"))
        b = mw._get_run_id(self._runtime(task_id="task-b"))
        assert a != b

    def test_lead_scope_is_still_the_run(self):
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        mw = TokenBudgetMiddleware.from_config(TokenBudgetConfig(enabled=True))
        assert mw._get_run_id(self._runtime()) == "run-1"

    def test_task_scope_differs_from_run_scope(self):
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        mw = TokenBudgetMiddleware.from_config(TokenBudgetConfig(enabled=True))
        assert mw._get_run_id(self._runtime(task_id="task-a")) != mw._get_run_id(self._runtime())
