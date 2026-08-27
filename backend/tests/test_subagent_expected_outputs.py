"""子代理自称 COMPLETED 不算完成:必需产物不存在即判 FAILED。

**故障**(thread `88df83a8`,task `call_01_FPXHRbeulZxpejOZGPFs0023`):EX 判定子代理没写
`judgments_draft_MCRC-2150006_EX.json`,而是自创 `qc_review_report.json`,并以
`completed` 返回一份 Markdown「QC 报告」。`task` 把 `completed` 当成功回给 lead,
lead 直到 8 分钟后自己跑结构闸才看到「闸1 文件不存在」,重派又撞上 run 中断,整轨作废。

本文件锁定 executor 侧的后置校验:
* 声明的产物缺失 / 为空 → `FAILED`(且 `stop_reason=None`,因此复用 `task` 现成的单次重试)
* 未声明 → 完全不碰 sandbox(向后兼容:今天所有调用方都没声明)
* 拿不到 sandbox → 跳过而不是误杀(不碰文件的子代理必须照常完成)

存在性探针用 ``Sandbox.download_file(path, max_bytes=...)``:它的契约要求 local 与 remote
两个实现在文件不存在时都抛 ``OSError``,是唯一 provider 无关的判据。``list_dir`` 对 local
sandbox 返回**宿主已解析路径**,与虚拟路径比不上,不能用。

注:conftest.py 用 MagicMock 顶替了 `deerflow.subagents.executor` 以打断循环导入,所以这里
沿用 `test_subagent_executor.py` 的 fixture 模式载入真实实现。
"""

from __future__ import annotations

import asyncio
import errno
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

_MOCKED_MODULE_NAMES = [
    "deerflow.agents",
    "deerflow.agents.thread_state",
    "deerflow.agents.middlewares",
    "deerflow.agents.middlewares.thread_data_middleware",
    "deerflow.sandbox",
    "deerflow.sandbox.middleware",
    "deerflow.sandbox.security",
    "deerflow.models",
    "deerflow.skills.storage",
]

DRAFT_EX = "/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_EX.json"
DRAFT_IN = "/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_IN.json"


@pytest.fixture(autouse=True)
def executor_classes():
    """Load the real executor module past conftest's circular-import stand-in."""
    original_modules = {name: sys.modules.get(name) for name in _MOCKED_MODULE_NAMES}
    original_executor = sys.modules.get("deerflow.subagents.executor")

    if "deerflow.subagents.executor" in sys.modules:
        del sys.modules["deerflow.subagents.executor"]
    subagents_pkg = sys.modules.get("deerflow.subagents")
    if subagents_pkg is not None and hasattr(subagents_pkg, "executor"):
        delattr(subagents_pkg, "executor")

    for name in _MOCKED_MODULE_NAMES:
        sys.modules[name] = MagicMock()
    storage_module = ModuleType("deerflow.skills.storage")
    storage_module.get_or_new_skill_storage = lambda **kwargs: SimpleNamespace(load_skills=lambda *, enabled_only: [])
    sys.modules["deerflow.skills.storage"] = storage_module

    from deerflow.subagents.config import SubagentConfig
    from deerflow.subagents.executor import SubagentExecutor, SubagentResult, SubagentStatus

    module = sys.modules["deerflow.subagents.executor"]
    module.get_app_config = lambda: SimpleNamespace(tool_search=SimpleNamespace(enabled=False))

    yield SimpleNamespace(
        module=module,
        SubagentConfig=SubagentConfig,
        SubagentExecutor=SubagentExecutor,
        SubagentResult=SubagentResult,
        SubagentStatus=SubagentStatus,
    )

    for name in _MOCKED_MODULE_NAMES:
        if original_modules[name] is not None:
            sys.modules[name] = original_modules[name]
        elif name in sys.modules:
            del sys.modules[name]
    if original_executor is not None:
        sys.modules["deerflow.subagents.executor"] = original_executor
    elif "deerflow.subagents.executor" in sys.modules:
        del sys.modules["deerflow.subagents.executor"]


class _FakeSandbox:
    """Records probes; mirrors the real ``download_file`` contract exactly.

    ``max_bytes`` is a **size limit, not a read window**: every real implementation
    (local / harness-aio / community-aio / agentrun) raises ``OSError(EFBIG)`` when the
    file is larger and returns nothing partial. An earlier version of this double
    truncated instead (``payload[:max_bytes]``), inverting the contract — which is how the
    EFBIG false negative shipped green. See ``TestOversizeArtifactCountsAsPresent``.
    """

    def __init__(self, files: dict[str, str], *, list_dir_raises: bool = False):
        self._files = files
        self.probes: list[tuple[str, int | None]] = []
        self.listings: list[str] = []
        self._list_dir_raises = list_dir_raises

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """Host-resolved paths, exactly like the local sandbox returns.

        The prefix deliberately differs from the ``/mnt/user-data`` virtual path so a test
        cannot accidentally pass by comparing full paths — only base names are usable.
        """
        self.listings.append(path)
        if self._list_dir_raises:
            raise OSError(errno.EACCES, "Permission denied", path)
        prefix = path.rstrip("/") + "/"
        return [f"/host/resolved{name}" for name in self._files if name.startswith(prefix) and "/" not in name[len(prefix) :]]

    def download_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self.probes.append((path, max_bytes))
        if path not in self._files:
            raise OSError(errno.ENOENT, "No such file or directory", path)
        payload = self._files[path].encode("utf-8")
        if max_bytes is not None and len(payload) > max_bytes:
            raise OSError(errno.EFBIG, f"File exceeds maximum download size of {max_bytes} bytes", path)
        return payload


def _make_executor(classes, *, expected_outputs, sandbox_state=None, files=None, provider_raises=False, monkeypatch=None, list_dir_raises=False):
    config = classes.SubagentConfig(name="general-purpose", description="d", system_prompt="s")
    executor = classes.SubagentExecutor(
        config=config,
        tools=[],
        sandbox_state=sandbox_state if sandbox_state is not None else {"sandbox_id": "local:u:t"},
        thread_id="t",
        expected_outputs=expected_outputs,
    )
    sandbox = _FakeSandbox(files or {}, list_dir_raises=list_dir_raises)

    def _provider():
        if provider_raises:
            raise RuntimeError("no provider in this process")
        return SimpleNamespace(get=lambda sandbox_id: sandbox)

    monkeypatch.setattr(classes.module, "_get_sandbox_provider", _provider, raising=True)
    return executor, sandbox


def _verify(executor, classes):
    return asyncio.run(executor._verify_expected_outputs())


class TestArtifactVerification:
    def test_all_present_and_non_empty_passes(self, executor_classes, monkeypatch):
        executor, sandbox = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX, DRAFT_IN],
            files={DRAFT_EX: '{"patient_id": "MCRC-2150006"}', DRAFT_IN: '{"a": 1}'},
            monkeypatch=monkeypatch,
        )
        assert _verify(executor, executor_classes) is None
        assert [p for p, _ in sandbox.probes] == [DRAFT_EX, DRAFT_IN]

    def test_missing_artifact_is_reported_with_its_path(self, executor_classes, monkeypatch):
        """The exact 88df83a8 shape: the other track's file exists, this one's does not."""
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX],
            files={DRAFT_IN: '{"ok": true}', "/mnt/user-data/workspace/patients/MCRC-2150006/qc_review_report.json": "{}"},
            monkeypatch=monkeypatch,
        )
        error = _verify(executor, executor_classes)
        assert error is not None
        assert DRAFT_EX in error
        assert "missing" in error

    @pytest.mark.parametrize("payload", ["", "   ", "\n", "{}", "[]"])
    def test_empty_artifact_counts_as_missing(self, executor_classes, monkeypatch, payload):
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX],
            files={DRAFT_EX: payload},
            monkeypatch=monkeypatch,
        )
        error = _verify(executor, executor_classes)
        assert error is not None
        assert DRAFT_EX in error

    def test_no_declaration_never_touches_the_sandbox(self, executor_classes, monkeypatch):
        executor, sandbox = _make_executor(executor_classes, expected_outputs=None, monkeypatch=monkeypatch)
        assert _verify(executor, executor_classes) is None
        assert sandbox.probes == [], "未声明的调用方不得因此多一次 IO"

    def test_missing_sandbox_state_skips_the_check(self, executor_classes, monkeypatch, caplog):
        executor, sandbox = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX],
            sandbox_state={},
            monkeypatch=monkeypatch,
        )
        assert _verify(executor, executor_classes) is None
        assert sandbox.probes == []

    def test_provider_failure_skips_rather_than_kills_the_task(self, executor_classes, monkeypatch):
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX],
            provider_raises=True,
            monkeypatch=monkeypatch,
        )
        assert _verify(executor, executor_classes) is None

    def test_probe_is_bounded(self, executor_classes, monkeypatch):
        """Existence + emptiness only: never pull a whole artifact into memory.

        The bound is asserted on the ``max_bytes`` the executor passes, not on the payload
        it gets back: real sandboxes refuse an oversize file outright rather than handing
        back a prefix, so a returned-bytes assertion cannot express "bounded".
        """
        executor, sandbox = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX],
            files={DRAFT_EX: '{"a": 1}'},
            monkeypatch=monkeypatch,
        )
        assert _verify(executor, executor_classes) is None
        assert all(max_bytes is not None and max_bytes <= 8192 for _, max_bytes in sandbox.probes)


class TestOversizeArtifactCountsAsPresent:
    """产物大于探测窗口 → ``OSError(EFBIG)`` → 必须判「存在且非空」,不得判 missing。

    **故障**(thread `9f069246`,run `66b363d4`):``criteria_parsed_IN.json`` 32,508 B 与
    ``criteria_parsed_EX.json`` 56,163 B 都已成功落盘(IN 的末步工具结果是
    ``OK: applied 11 patch(es) ... sha256 ed4f1955dd77``),闸仍报
    ``missing=['/mnt/user-data/workspace/criteria_parsed_IN.json']``。三个轨道任务全中招,
    IN 白烧两轮(1.55M + 0.49M token),EX 一轮 0.80M。

    根因:``max_bytes`` 是**尺寸上限**而非截断读 —— ``local_sandbox.py`` 先
    ``os.path.getsize`` 再 ``raise OSError(errno.EFBIG)``,而探针把所有 ``OSError``
    一律当「不存在」。于是 >4 KB 的产物必然误判,闸对它本该守护的结构化 JSON 场景永远
    误报,只有 <4 KB 的小文件才正常工作。

    ``EFBIG`` 是「文件存在**且**大于探测窗口」的确证,即非空产物的最强判据 —— 它是唯一
    应当直接判通过的 ``OSError``,``ENOENT``/其余 errno 仍按缺失处理。
    """

    def test_oversize_artifact_is_not_reported_missing(self, executor_classes, monkeypatch):
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN],
            files={DRAFT_IN: "x" * 32_508},  # 真实 criteria_parsed_IN.json 的字节数
            monkeypatch=monkeypatch,
        )
        assert _verify(executor, executor_classes) is None

    def test_probe_window_is_smaller_than_the_artifacts_it_guards(self, executor_classes):
        """守卫的产物普遍远大于探测窗口,所以 EFBIG 是常态而非边缘情况。"""
        executor = executor_classes.SubagentExecutor(
            config=executor_classes.SubagentConfig(name="general-purpose", description="d", system_prompt="s"),
            tools=[],
            sandbox_state={"sandbox_id": "local:u:t"},
            thread_id="t",
            expected_outputs=[DRAFT_IN],
        )
        assert executor._EXPECTED_OUTPUT_PROBE_BYTES < 32_508

    def test_missing_still_fails_when_oversize_passes(self, executor_classes, monkeypatch):
        """放行 EFBIG 不得放行 ENOENT:88df83a8 那种真缺失仍必须判 failed。"""
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_EX, DRAFT_IN],
            files={DRAFT_IN: "x" * 32_508},  # oversize → present; DRAFT_EX 真缺失
            monkeypatch=monkeypatch,
        )
        error = _verify(executor, executor_classes)
        assert error is not None
        assert DRAFT_EX in error
        assert DRAFT_IN not in error

    def test_other_oserrors_still_count_as_missing(self, executor_classes, monkeypatch):
        """只有 EFBIG 例外:EACCES 之类既非「存在」的证据,也不该静默放行。"""

        class _DeniedSandbox:
            def download_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
                raise PermissionError(errno.EACCES, "Access denied", path)

        config = executor_classes.SubagentConfig(name="general-purpose", description="d", system_prompt="s")
        executor = executor_classes.SubagentExecutor(
            config=config,
            tools=[],
            sandbox_state={"sandbox_id": "local:u:t"},
            thread_id="t",
            expected_outputs=[DRAFT_EX],
        )
        monkeypatch.setattr(
            executor_classes.module,
            "_get_sandbox_provider",
            lambda: SimpleNamespace(get=lambda sandbox_id: _DeniedSandbox()),
            raising=True,
        )
        error = _verify(executor, executor_classes)
        assert error is not None
        assert DRAFT_EX in error


class TestFailureShape:
    """失败必须能复用 task 现成的单次重试,并能被离线分析识别。"""

    def test_error_text_carries_the_greppable_marker(self, executor_classes, monkeypatch):
        from deerflow.tools.builtins.task_tool import EXPECTED_OUTPUTS_FAILURE_MARKER

        executor, _ = _make_executor(executor_classes, expected_outputs=[DRAFT_EX], monkeypatch=monkeypatch)
        error = _verify(executor, executor_classes)
        assert error is not None
        assert EXPECTED_OUTPUTS_FAILURE_MARKER in error

    def test_failure_is_retryable(self, executor_classes):
        """``stop_reason=None`` 才会走 task 的重试分支(资源上限类才不重试)。"""
        from deerflow.config.subagents_config import SubagentsAppConfig
        from deerflow.tools.builtins.task_tool import _is_retryable_failure

        config = SimpleNamespace(subagents=SubagentsAppConfig())
        assert _is_retryable_failure(None, config) is True


class TestEndToEndThroughExecutor:
    """走完整 ``_aexecute`` 路径复现 88df83a8:子代理"成功"但没落盘。"""

    def _run(self, classes, monkeypatch, *, files):
        from langchain_core.messages import AIMessage

        executor, sandbox = _make_executor(
            classes,
            expected_outputs=[DRAFT_EX],
            files=files,
            monkeypatch=monkeypatch,
        )

        # Stand in for the agent: it "finishes" with a QC report, exactly like the real
        # subagent did, without ever writing the artifact it was told to produce.
        final_text = "## 📋 MCRC-2150006 完整QC判定报告\n完整JSON报告已保存至 qc_review_report.json"

        class _Agent:
            async def astream(self, state, config=None, context=None, stream_mode=None):
                yield {"messages": [AIMessage(content=final_text)]}

        monkeypatch.setattr(executor, "_create_agent", lambda *a, **k: _Agent())

        async def _fake_initial_state(task):
            return {"messages": []}, [], None

        monkeypatch.setattr(executor, "_build_initial_state", _fake_initial_state)
        result = asyncio.run(executor._aexecute("判定 EX 轨"))
        return result, sandbox, final_text

    def test_completion_without_the_artifact_becomes_failed(self, executor_classes, monkeypatch):
        result, _, _ = self._run(
            executor_classes,
            monkeypatch,
            files={"/mnt/user-data/workspace/patients/MCRC-2150006/qc_review_report.json": '{"verdict": "..."}'},
        )
        assert result.status is executor_classes.SubagentStatus.FAILED
        assert result.error is not None
        assert DRAFT_EX in result.error
        # No stop_reason => task_tool spends its one retry, which is what the lead had to
        # do by hand 8 minutes later in the real run.
        assert result.stop_reason is None

    def test_completion_with_the_artifact_stays_completed(self, executor_classes, monkeypatch):
        result, _, final_text = self._run(
            executor_classes,
            monkeypatch,
            files={DRAFT_EX: '{"patient_id": "MCRC-2150006", "documents": {}}'},
        )
        assert result.status is executor_classes.SubagentStatus.COMPLETED
        assert result.result == final_text


# --------------------------------------------------------------------------- #
# 失败信息要说明「产物去哪了」(thread 7512ebd2)                                 #
# --------------------------------------------------------------------------- #
#
# `missing=[...]` 只说了产物不在,没说**为什么**不在,而两种成因需要相反的处置:
# 判定确实没做 → 重派;判定做对了只是存成 `judgment_IN.md` / `judgments_EX.json`
# → 内容还在,不必重跑 4 小时。7512ebd2 里 lead 每次失败都要自己 ls + cat 逐轨排查
# (seq 1163-1173 共 5 轮),而这份信息闸自己一次 list_dir 就拿得到。


class TestFailureNamesTheStrayArtifacts:
    def test_missing_output_lists_what_the_subagent_actually_wrote(self, executor_classes, monkeypatch):
        """7512ebd2 的真实形态:自创文件名躺在声明路径旁边。"""
        directory = "/mnt/user-data/workspace/patients/MCRC-2150006"
        executor, sandbox = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN],
            files={
                f"{directory}/judgment_IN.md": "# 入选标准核验结果",
                f"{directory}/judgments_EX.json": '{"四分类": {}}',
            },
            monkeypatch=monkeypatch,
        )

        error = _verify(executor, executor_classes)

        assert error is not None
        assert "judgment_IN.md" in error, "改名而非漏写这一情况必须在失败信息里可见"
        assert "judgments_EX.json" in error
        assert sandbox.listings == [directory], "只列声明路径所在目录,不做全盘扫描"

    def test_listing_reports_base_names_not_host_paths(self, executor_classes, monkeypatch):
        """local sandbox 的 list_dir 返回宿主路径,原样贴进去会给 lead 一条不可用路径。"""
        directory = "/mnt/user-data/workspace/patients/MCRC-2150006"
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN],
            files={f"{directory}/judgment_IN.md": "x"},
            monkeypatch=monkeypatch,
        )

        error = _verify(executor, executor_classes)

        assert error is not None
        assert "/host/resolved" not in error

    def test_directory_is_listed_once_for_two_missing_siblings(self, executor_classes, monkeypatch):
        directory = "/mnt/user-data/workspace/patients/MCRC-2150006"
        executor, sandbox = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN, DRAFT_EX],
            files={f"{directory}/judgment_IN.md": "x"},
            monkeypatch=monkeypatch,
        )

        assert _verify(executor, executor_classes) is not None
        assert sandbox.listings == [directory]

    def test_empty_directory_adds_no_listing_clause(self, executor_classes, monkeypatch):
        """真的什么都没写时,不要挂一句空清单——那会读成「有文件但我没告诉你」。"""
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN],
            files={},
            monkeypatch=monkeypatch,
        )

        error = _verify(executor, executor_classes)

        assert error is not None
        assert "Files that DO exist" not in error

    def test_listing_failure_never_masks_the_real_error(self, executor_classes, monkeypatch):
        """诊断性附注不得把闸本身变成一个会抛异常的东西。"""
        executor, _ = _make_executor(
            executor_classes,
            expected_outputs=[DRAFT_IN],
            files={"/mnt/user-data/workspace/patients/MCRC-2150006/judgment_IN.md": "x"},
            list_dir_raises=True,
            monkeypatch=monkeypatch,
        )

        error = _verify(executor, executor_classes)

        assert error is not None
        assert DRAFT_IN in error
        assert "missing" in error
