"""同一任务内重复**整份**读同一个大文件必须被拦下。

**故障链**(thread `88df83a8`,EX 判定 task `call_01_FPXHRbeulZxpejOZGPFs0023`):
`ocr_records.md`(7,604 行)整份读了 6 次,`whole_file_read_calls=10`、
`range_overlap_lines=936`,`tokens_before` 冲到 99,755(压缩触发线 60k 的 1.66 倍)。
任务内因此压缩 4 次,随后子代理丢失工作状态、改写了自己的任务目标。

为什么 `ReadFileDedupMiddleware` 兜不住:它按 `(path, start_line, end_line, content_hash)`
命中,而"整份读 + 分段读"每次的 key 都不同,内容对缓存都是"新"的。dedup 在该会话只命中
`dedupable_read_calls=1`,却有 10 次整份读。

委派模板早就写了「每份输入文件本任务内最多 read_file 一次」——本会话违反 6 次。
散文管不住,所以做成机械的。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.read_file_policy_middleware import (
    ReadFilePolicyMiddleware,
    _reset_read_file_policy_cache,
)
from deerflow.config.read_file_policy_config import ReadFilePolicyConfig

OCR = "/mnt/user-data/workspace/patients/MCRC-2150006/ocr/筛选期检查/ocr_records.md"
BIG_BODY = "[lines 1-7604 of 7604]\n" + "\n".join(f"line {i}" for i in range(7604))
SMALL_BODY = "[lines 1-40 of 40]\n" + "\n".join(f"line {i}" for i in range(40))


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_read_file_policy_cache()
    yield
    _reset_read_file_policy_cache()


def _request(path: str, *, task_id: str | None = "task-1", start_line=None, end_line=None, tool="read_file"):
    args: dict = {"path": path}
    if start_line is not None:
        args["start_line"] = start_line
    if end_line is not None:
        args["end_line"] = end_line
    return SimpleNamespace(
        tool_call={"name": tool, "args": args, "id": "call-x"},
        runtime=SimpleNamespace(context={"sandbox_id": "sb", "thread_id": "t", "run_id": "r", "task_id": task_id}),
        state={"messages": []},
    )


def _handler(body: str):
    def handler(request):
        return ToolMessage(content=body, tool_call_id=request.tool_call["id"], name="read_file")

    return handler


def _middleware(**overrides) -> ReadFilePolicyMiddleware:
    config = ReadFilePolicyConfig(enabled=True, **overrides)
    return ReadFilePolicyMiddleware(config=config)


class TestBlockMode:
    def test_first_whole_file_read_passes_through(self):
        result = _middleware().wrap_tool_call(_request(OCR), _handler(BIG_BODY))
        assert result.content == BIG_BODY

    def test_second_whole_file_read_is_refused_with_an_alternative(self):
        middleware = _middleware()
        middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))

        handler_calls: list = []

        def counting_handler(request):
            handler_calls.append(request)
            return ToolMessage(content=BIG_BODY, tool_call_id="call-x", name="read_file")

        result = middleware.wrap_tool_call(_request(OCR), counting_handler)
        assert handler_calls == [], "被拦的调用不应到达 sandbox"
        assert result.content.startswith("Error:")
        assert "7604" in result.content, "必须告诉模型这份文件有多大"
        assert "grep" in result.content and "start_line" in result.content, "必须给出可执行替代"
        assert OCR in result.content

    def test_ranged_reads_are_never_blocked(self):
        middleware = _middleware()
        middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))
        for start, end in ((1, 500), (500, 1000), (6470, 7200)):
            result = middleware.wrap_tool_call(
                _request(OCR, start_line=start, end_line=end),
                _handler("[lines x-y of 7604]\npartial"),
            )
            assert not result.content.startswith("Error:")

    def test_small_files_are_not_governed(self):
        middleware = _middleware()
        for _ in range(3):
            result = middleware.wrap_tool_call(_request("/mnt/user-data/workspace/criteria_meta.json"), _handler(SMALL_BODY))
            assert result.content == SMALL_BODY

    def test_threshold_is_inclusive(self):
        body = "\n".join(f"line {i}" for i in range(1500))
        middleware = _middleware(min_lines_for_ranged=1500)
        middleware.wrap_tool_call(_request(OCR), _handler(body))
        result = middleware.wrap_tool_call(_request(OCR), _handler(body))
        assert result.content.startswith("Error:")

    def test_other_tasks_are_isolated(self):
        """子代理上下文互相看不见,不能因为别的任务读过就拦这个任务的首读。"""
        middleware = _middleware()
        middleware.wrap_tool_call(_request(OCR, task_id="task-A"), _handler(BIG_BODY))
        result = middleware.wrap_tool_call(_request(OCR, task_id="task-B"), _handler(BIG_BODY))
        assert result.content == BIG_BODY

    def test_other_paths_are_independent(self):
        middleware = _middleware()
        middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))
        other = "/mnt/user-data/workspace/patients/MCRC-2150006/ocr/筛选期病历/ocr_records.md"
        assert middleware.wrap_tool_call(_request(other), _handler(BIG_BODY)).content == BIG_BODY

    def test_error_results_are_not_recorded(self):
        """首读失败不能算作"读过",否则一次瞬时错误会永久拦住这个文件。"""
        middleware = _middleware()
        middleware.wrap_tool_call(_request(OCR), _handler("Error: File not found: " + OCR))
        assert middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY)).content == BIG_BODY

    def test_non_read_tools_are_untouched(self):
        middleware = _middleware()
        for tool in ("write_file", "bash", "grep"):
            result = middleware.wrap_tool_call(_request(OCR, tool=tool), _handler("ok"))
            assert result.content == "ok"


class TestWarnMode:
    def test_content_is_returned_with_guidance_appended(self):
        middleware = _middleware(mode="warn")
        middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))
        result = middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))
        assert result.content.startswith(BIG_BODY)
        assert "grep" in result.content
        assert not result.content.startswith("Error:")


class TestDisabled:
    def test_disabled_config_is_a_pure_passthrough(self):
        middleware = ReadFilePolicyMiddleware(config=ReadFilePolicyConfig(enabled=False))
        for _ in range(3):
            assert middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY)).content == BIG_BODY


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_async_path_matches_sync_path(self):
        """生产走异步:仓库有过"14 项测试全是同步"的教训。"""
        middleware = _middleware()

        async def ahandler(request):
            return ToolMessage(content=BIG_BODY, tool_call_id="call-x", name="read_file")

        first = await middleware.awrap_tool_call(_request(OCR), ahandler)
        assert first.content == BIG_BODY
        second = await middleware.awrap_tool_call(_request(OCR), ahandler)
        assert second.content.startswith("Error:")
        assert "7604" in second.content


class TestSessionReplay:
    def test_replaying_88df83a8_reduces_six_whole_reads_to_one(self):
        """回放该会话的读取序列:6 次整份读只有第 1 次放行。"""
        middleware = _middleware()
        allowed = 0
        for _ in range(6):
            result = middleware.wrap_tool_call(_request(OCR), _handler(BIG_BODY))
            if not result.content.startswith("Error:"):
                allowed += 1
        assert allowed == 1


class TestNoGraphNode:
    """`config.yaml:339-363` 的纪律:加图节点的 middleware 会静默削减 max_turns。

    实测倍率 `recursion_limit / 真实回合` = 4.03–4.05,每加一个带 `before_model` /
    `after_model` 的 middleware 就上升一档。本 middleware 只用 `wrap_tool_call`,
    因此不加节点、倍率不变 —— 这条断言把该性质钉住,防止以后有人顺手加个 `before_model`。
    """

    def test_only_tool_call_wrappers_are_implemented(self):
        from langchain.agents.middleware import AgentMiddleware

        node_hooks = ("before_model", "after_model", "abefore_model", "aafter_model", "before_agent", "after_agent")
        overridden = [h for h in node_hooks if getattr(ReadFilePolicyMiddleware, h, None) is not getattr(AgentMiddleware, h, None)]
        assert overridden == [], f"这些钩子会新增图节点并压缩 max_turns: {overridden}"

        wrappers = ("wrap_tool_call", "awrap_tool_call")
        implemented = [h for h in wrappers if getattr(ReadFilePolicyMiddleware, h, None) is not getattr(AgentMiddleware, h, None)]
        assert implemented == list(wrappers)


class TestChainMounting:
    @staticmethod
    def _app_config(**overrides):
        # Same construction the read_before_write wiring tests use: AppConfig requires
        # `sandbox`, and depending on the local gitignored config.yaml would make this
        # unrunnable in CI.
        from deerflow.config.app_config import AppConfig
        from deerflow.config.sandbox_config import SandboxConfig

        return AppConfig(sandbox=SandboxConfig(use="test"), **overrides)

    def test_policy_is_ordered_before_dedup(self):
        """被拦的调用不该到达 sandbox,dedup 的账本也只应看到真实发生过的读。"""
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares, build_subagent_runtime_middlewares
        from deerflow.config.read_dedup_config import ReadFileDedupConfig

        config = self._app_config(
            read_file_policy=ReadFilePolicyConfig(enabled=True),
            read_file_dedup=ReadFileDedupConfig(enabled=True),
        )
        for build in (build_lead_runtime_middlewares, build_subagent_runtime_middlewares):
            names = [type(m).__name__ for m in build(app_config=config)]
            assert "ReadFilePolicyMiddleware" in names
            assert names.index("ReadFilePolicyMiddleware") < names.index("ReadFileDedupMiddleware")

    def test_disabled_config_mounts_nothing(self):
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

        names = [type(m).__name__ for m in build_lead_runtime_middlewares(app_config=self._app_config())]
        assert "ReadFilePolicyMiddleware" not in names
