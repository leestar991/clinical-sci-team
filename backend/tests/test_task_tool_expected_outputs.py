"""``task(expected_outputs=[...])`` — declare a subagent's required artifacts.

**故障**:会话 `88df83a8` 的 EX 判定子代理没写 `judgments_draft_MCRC-2150006_EX.json`,
却自创 `qc_review_report.json` 并以 `completed` 返回。`task` 无条件把 `completed` 当成功,
lead 直到 8 分钟后自己跑结构闸才发现产物不存在,重派又撞上 run 中断,整轨判定作废。

`expected_outputs` 把「产物必须存在」从 prompt 里的一句话变成机械后置条件。本文件锁定
**工具边界**的行为(路径合法性、去重、上限、透传);落盘校验本身在
`test_subagent_expected_outputs.py`。

边界校验必须发生在**派任务之前**:一个写错的声明如果等到子代理跑完才报错,代价是白烧一整个
子任务的额度;而且不能允许把校验指向宿主路径 —— 那等于让模型隔着 sandbox 探测宿主文件系统。
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def task_tool_module():
    return importlib.import_module("deerflow.tools.builtins.task_tool")


class TestNormalizeExpectedOutputs:
    """``_normalize_expected_outputs`` 返回 ``(paths, error)``,两者互斥。"""

    def test_none_and_empty_are_accepted_as_no_declaration(self, task_tool_module):
        for value in (None, []):
            paths, error = task_tool_module._normalize_expected_outputs(value)
            assert error is None
            assert paths == []

    def test_valid_sandbox_paths_pass_through(self, task_tool_module):
        declared = [
            "/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_EX.json",
            "/mnt/user-data/outputs/report.html",
        ]
        paths, error = task_tool_module._normalize_expected_outputs(declared)
        assert error is None
        assert paths == declared

    def test_duplicates_are_removed_preserving_order(self, task_tool_module):
        a = "/mnt/user-data/workspace/a.json"
        b = "/mnt/user-data/workspace/b.json"
        paths, error = task_tool_module._normalize_expected_outputs([a, b, a])
        assert error is None
        assert paths == [a, b]

    @pytest.mark.parametrize(
        "bad",
        [
            "workspace/a.json",  # relative
            "/etc/passwd",  # host absolute
            "/Users/louli/secret.json",  # host absolute
            "/mnt/skills/custom/x.json",  # inside the sandbox but not user data
            "/mnt/user-data/../../etc/passwd",  # traversal
            "",  # empty
            "   ",  # whitespace
        ],
    )
    def test_paths_outside_user_data_are_rejected(self, task_tool_module, bad):
        paths, error = task_tool_module._normalize_expected_outputs([bad])
        assert paths == []
        assert error is not None
        assert "/mnt/user-data/" in error

    def test_too_many_entries_are_rejected(self, task_tool_module):
        declared = [f"/mnt/user-data/workspace/f{i}.json" for i in range(11)]
        paths, error = task_tool_module._normalize_expected_outputs(declared)
        assert paths == []
        assert error is not None
        assert str(task_tool_module.EXPECTED_OUTPUTS_LIMIT) in error

    def test_limit_boundary_is_inclusive(self, task_tool_module):
        declared = [f"/mnt/user-data/workspace/f{i}.json" for i in range(task_tool_module.EXPECTED_OUTPUTS_LIMIT)]
        paths, error = task_tool_module._normalize_expected_outputs(declared)
        assert error is None
        assert len(paths) == task_tool_module.EXPECTED_OUTPUTS_LIMIT

    def test_non_string_entries_are_rejected(self, task_tool_module):
        paths, error = task_tool_module._normalize_expected_outputs([{"path": "/mnt/user-data/a.json"}])
        assert paths == []
        assert error is not None


class TestToolBoundary:
    """非法声明必须在实例化 executor 之前失败。"""

    @pytest.fixture
    def spy_executor(self, task_tool_module, monkeypatch):
        created: list = []

        class _Spy:
            def __init__(self, **kwargs):
                created.append(kwargs)

            def execute_async(self, prompt, task_id=None):  # pragma: no cover - must not run
                raise AssertionError("subagent must not be dispatched")

        monkeypatch.setattr(task_tool_module, "SubagentExecutor", _Spy, raising=False)
        return created

    @pytest.mark.asyncio
    async def test_invalid_declaration_short_circuits_before_dispatch(self, task_tool_module, spy_executor):
        result = await task_tool_module.task_tool.coroutine(
            runtime=None,
            description="EX judgment",
            prompt="判定",
            subagent_type="general-purpose",
            tool_call_id="call_1",
            expected_outputs=["/etc/passwd"],
        )
        assert result.startswith("Error:")
        assert "/mnt/user-data/" in result
        assert spy_executor == [], "非法参数不得消耗子代理额度"

    @pytest.mark.asyncio
    async def test_unknown_subagent_type_still_reports_first(self, task_tool_module, spy_executor):
        """未知类型的报错优先级不受新参数影响(既有行为不回退)。"""
        result = await task_tool_module.task_tool.coroutine(
            runtime=None,
            description="x",
            prompt="y",
            subagent_type="no-such-agent",
            tool_call_id="call_2",
            expected_outputs=["/etc/passwd"],
        )
        assert "Unknown subagent type" in result
        assert spy_executor == []


class TestSchemaExposure:
    """声明必须出现在工具 schema 里,否则模型永远不会传它。

    executor 侧的接收与校验由 ``test_subagent_expected_outputs.py`` 覆盖(那里需要绕过
    conftest 对 ``deerflow.subagents.executor`` 的循环导入替身)。
    """

    def test_parameter_is_declared_in_args_schema(self, task_tool_module):
        # tool_call_schema is what the model is shown (args_schema also carries the
        # injected Runtime, which is not JSON-schema serializable).
        schema = task_tool_module.task_tool.tool_call_schema.model_json_schema()
        assert "expected_outputs" in schema["properties"]
        described = schema["properties"]["expected_outputs"].get("description") or ""
        assert "/mnt/user-data/" in described
        assert str(task_tool_module.EXPECTED_OUTPUTS_LIMIT) in described

    def test_parameter_is_optional(self, task_tool_module):
        schema = task_tool_module.task_tool.tool_call_schema.model_json_schema()
        assert "expected_outputs" not in schema.get("required", [])
