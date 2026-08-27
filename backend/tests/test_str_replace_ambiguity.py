"""``str_replace`` 歧义防护:``replace_all=False`` 且 ``old_str`` 多处出现时必须拒绝。

背景(criteria-token-saving-v1.2 Task 5 Step 0b):`str_replace_tool` 的 docstring 明写
"If ``replace_all`` is False (default), the substring to replace must appear **exactly once**
in the file",但实现是 ``content.replace(old_str, new_str, 1)`` —— **不校验出现次数,静默替换
第一处**。文档与行为直接矛盾。

为什么必须修:eligibility-judgment 的改判手册要求「一条 blocking_issue → 一次 ``str_replace``」
(`references/judgment-repair.md`),且判定 JSON 里同一段 reason/conclusion 文本极易重复出现。
静默替换第一处 = 改到了错误的条目,而条目数守恒闸与结构闸都看不出来(总条数没变、JSON 仍合法)。
这类静默改错正是该技能反复出事故的模式。Claude Code 的 Edit 工具在多处出现时报错,本工具应对齐。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deerflow.sandbox.tools import str_replace_tool


class FakeSandbox:
    """最小 Sandbox:只实现 str_replace 用到的 read_file / write_file。"""

    def __init__(self, content: str):
        self.content = content
        self.writes: list[str] = []

    @property
    def id(self) -> str:
        return "sandbox-test"

    def read_file(self, path: str) -> str:
        return self.content

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.content = content
        self.writes.append(content)


@pytest.fixture
def runtime() -> SimpleNamespace:
    return SimpleNamespace(state={}, context={"thread_id": "thread-1"}, config={})


@pytest.fixture(autouse=True)
def _stub_sandbox_plumbing(monkeypatch):
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda runtime: None)
    monkeypatch.setattr("deerflow.sandbox.tools.is_local_sandbox", lambda runtime: False)


def _run(monkeypatch, sandbox: FakeSandbox, runtime, old: str, new: str, *, replace_all: bool = False) -> str:
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda r: sandbox)
    return str_replace_tool.func(
        runtime,
        "test edit",
        "/mnt/user-data/workspace/f.json",
        old,
        new,
        replace_all,
    )


# --- 核心:多处出现必须拒绝 ---------------------------------------------------


def test_rejects_when_old_str_occurs_more_than_once(monkeypatch, runtime):
    sandbox = FakeSandbox('{"a": "未触发该排除条件", "b": "未触发该排除条件"}')
    before = sandbox.content

    result = _run(monkeypatch, sandbox, runtime, "未触发该排除条件", "触发该排除条件")

    assert result.startswith("Error:"), f"多处出现时必须报错，实际返回 {result!r}"
    assert "2" in result, "错误信息应告知实际出现次数，便于模型缩小 old_str"
    assert sandbox.writes == [], "拒绝时不得写入文件"
    assert sandbox.content == before, "拒绝时文件内容必须保持原样"


def test_error_message_guides_disambiguation(monkeypatch, runtime):
    sandbox = FakeSandbox("x\nx\nx\n")
    result = _run(monkeypatch, sandbox, runtime, "x", "y")
    assert result.startswith("Error:")
    assert "3" in result
    # 必须给出可执行的下一步，而不是只说失败
    assert "replace_all" in result, "应提示 replace_all=True 或扩大 old_str 上下文"


# --- 不得破坏既有正常路径 ---------------------------------------------------


def test_single_occurrence_still_replaces(monkeypatch, runtime):
    sandbox = FakeSandbox('{"conclusion": "符合", "note": "x"}')
    result = _run(monkeypatch, sandbox, runtime, '"符合"', '"不符合"')
    assert result == "OK"
    assert sandbox.content == '{"conclusion": "不符合", "note": "x"}'


def test_replace_all_true_replaces_every_occurrence(monkeypatch, runtime):
    sandbox = FakeSandbox("a a a")
    result = _run(monkeypatch, sandbox, runtime, "a", "b", replace_all=True)
    assert result == "OK"
    assert sandbox.content == "b b b"


def test_not_found_message_unchanged(monkeypatch, runtime):
    sandbox = FakeSandbox("hello")
    result = _run(monkeypatch, sandbox, runtime, "missing", "x")
    assert "String to replace not found" in result
    assert sandbox.writes == []


def test_empty_file_still_returns_ok(monkeypatch, runtime):
    """空文件的既有短路行为不变(返回 OK 且不写)。"""
    sandbox = FakeSandbox("")
    result = _run(monkeypatch, sandbox, runtime, "a", "b")
    assert result == "OK"
    assert sandbox.writes == []


def test_overlapping_occurrences_counted_like_python_count(monkeypatch, runtime):
    """计数口径与 str.count 一致(非重叠)，避免与 replace 的语义错位。"""
    sandbox = FakeSandbox("aaaa")  # "aa".count -> 2 (非重叠)
    result = _run(monkeypatch, sandbox, runtime, "aa", "b")
    assert result.startswith("Error:")
    assert "2" in result
