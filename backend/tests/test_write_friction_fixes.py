"""两处摩擦修复，均来自 thread `dfbb4554` 的实证（模型被逼写生成器脚本改写判定产物）。

trace 里的实际序列（EX 判定任务，步 470–516）：

    [470] bash        → Command blocked: command too long          ← heredoc 超 10,000 字符
    [483] bash        → Command blocked: command too long          ← 再试仍超
    [486] write_file  → OK                                          ← 创建 build_judgments.py
    [488] write_file  → ❌ blocked — already exists and you have not read its current version
    [489] read_file   → 读回自己 3 秒前刚写的脚本                    ← 纯摩擦
    [492] write_file  → OK
    [494] write_file  → ❌ 同一个闸再拦一次
    [495] read_file   → 再读一遍
    [502] bash        → Command blocked: command too long           ← 第三次撞墙
    [505] AI「broken into write_file calls (each under 80K)」        ← 自己摸索出分片方案

两个独立缺陷：

**A. read-before-write 对"本 run 内自己刚创建的文件"也拦。**
闸的价值是"别盲改你没看过的内容"。但模型自己 `write_file` 刚创建的文件，内容就是它自己写的、
在上下文里，再要求 `read_file` 读一遍毫无信息增益，只是多烧两轮 AI step。
修法：`write_file` 创建/写入成功后为该文件打上 mark（哈希取写入后的实际内容），
后续修改即可直接过闸。⚠️ 不能因此放过"未读过的既有文件"——那才是闸要防的。

**B. bash 超长的拒绝信息不含可执行替代方案。**
原文案只有 "Command blocked: command too long. Please use a safer alternative approach."
——既不说实际长度、也不说上限、更不指方向，模型只能自己试。三次撞墙后才摸到分片 write_file。
修法：带上实际长度与上限，并直接指向 `write_file`（分片 append）与 `apply_json_patches`。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.read_before_write_middleware import (
    READ_MARK_KEY,
    ReadBeforeWriteMiddleware,
)
from deerflow.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware

PATH = "/mnt/user-data/workspace/build_judgments.py"


# ─────────────────────────── A. read-before-write 豁免 ───────────────────────────


class Recorder:
    """记录 handler 是否被调用（= 闸是否放行），并模拟真实工具的落盘副作用。

    落盘必须发生在 handler 内部：打 mark 是在 handler 返回后、`wrap_tool_call` 返回前完成的，
    它会重新读一次沙箱取哈希。若在 `wrap_tool_call` 之后才写 files，打 mark 时文件还不存在。
    """

    def __init__(self, result: str = "OK", *, files: dict[str, str] | None = None):
        self.calls = 0
        self.result = result
        self._files = files

    def __call__(self, request):
        self.calls += 1
        if self._files is not None and not self.result.startswith("Error:"):
            args = request.tool_call.get("args") or {}
            path = args.get("path")
            if "content" in args:
                self._files[path] = args["content"]
            elif "old_str" in args:  # str_replace
                cur = self._files.get(path, "")
                self._files[path] = cur.replace(args["old_str"], args["new_str"], 1)
        return ToolMessage(content=self.result, tool_call_id="c1", name=request.tool_call.get("name"))


def make_request(tool: str, path: str, *, messages=None, content: str = "print(1)\n"):
    return SimpleNamespace(
        tool_call={"name": tool, "args": {"path": path, "content": content}, "id": "c1"},
        state={"messages": list(messages or [])},
        runtime=SimpleNamespace(context={"thread_id": "t1"}, state={}),
    )


def mw_with_content(current: dict[str, str]) -> ReadBeforeWriteMiddleware:
    """content_reader 从 dict 取当前内容；缺失即 FileNotFoundError（= 文件不存在）。"""

    def reader(_runtime, path: str) -> str:
        if path not in current:
            raise FileNotFoundError(path)
        return current[path]

    return ReadBeforeWriteMiddleware(content_reader=reader)


def test_creating_a_new_file_then_rewriting_it_is_not_blocked():
    """核心回归：write_file 创建 → 再 write_file 改，不应被要求先 read_file。"""
    files: dict[str, str] = {}
    mw = mw_with_content(files)

    # 第一次写：文件不存在 → 闸放行（既有行为）
    first = Recorder(files=files)
    req1 = make_request("write_file", PATH, content="v1")
    result1 = mw.wrap_tool_call(req1, first)
    assert first.calls == 1
    assert files[PATH] == "v1"

    # 第二次写：文件已存在，但它是本 run 自己刚创建的 → 应放行
    second = Recorder(files=files)
    req2 = make_request("write_file", PATH, messages=[result1], content="v2")
    result2 = mw.wrap_tool_call(req2, second)

    assert not (isinstance(result2, ToolMessage) and result2.status == "error"), f"自己刚创建的文件被闸拦住了：{getattr(result2, 'content', result2)}"
    assert second.calls == 1, "闸应放行，handler 必须被调用"


def test_write_stamps_a_mark_so_the_next_write_passes():
    """放行的机制是 write_file 成功后打 mark（哈希 = 写入后的实际内容）。"""
    files: dict[str, str] = {}
    mw = mw_with_content(files)
    req = make_request("write_file", PATH, content="v1")
    result = mw.wrap_tool_call(req, Recorder(files=files))

    mark = (result.additional_kwargs or {}).get(READ_MARK_KEY)
    assert isinstance(mark, dict), "write_file 成功后必须打 read mark"
    assert mark.get("hash") == hashlib.sha256(b"v1").hexdigest(), "mark 的哈希必须等于写入后的内容"


def test_existing_unread_file_is_still_blocked():
    """豁免不得扩大：本 run 没碰过的既有文件，仍必须先读——这才是闸的本职。"""
    files = {PATH: "pre-existing content from an earlier run"}
    mw = mw_with_content(files)
    handler = Recorder()
    result = mw.wrap_tool_call(make_request("write_file", PATH, content="overwrite"), handler)

    assert isinstance(result, ToolMessage) and result.status == "error", "未读过的既有文件必须仍被拦"
    assert handler.calls == 0


def test_stale_mark_after_external_change_is_still_blocked():
    """写后文件被外部改动 → 旧 mark 失效，必须重读。"""
    files: dict[str, str] = {}
    mw = mw_with_content(files)
    first = mw.wrap_tool_call(make_request("write_file", PATH, content="v1"), Recorder(files=files))
    files[PATH] = "changed by something else"

    handler = Recorder()
    result = mw.wrap_tool_call(make_request("write_file", PATH, messages=[first], content="v2"), handler)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert handler.calls == 0


def test_str_replace_after_own_write_is_not_blocked():
    """同一豁免要覆盖 str_replace —— 改判场景正是 write 之后紧接 str_replace。"""
    files: dict[str, str] = {}
    mw = mw_with_content(files)
    first = mw.wrap_tool_call(make_request("write_file", PATH, content="v1"), Recorder(files=files))
    assert files[PATH] == "v1"

    handler = Recorder(files=files)
    req = SimpleNamespace(
        tool_call={"name": "str_replace", "args": {"path": PATH, "old_str": "v1", "new_str": "v2"}, "id": "c2"},
        state={"messages": [first]},
        runtime=SimpleNamespace(context={"thread_id": "t1"}, state={}),
    )
    result = mw.wrap_tool_call(req, handler)
    assert handler.calls == 1, f"被拦：{getattr(result, 'content', result)}"


def test_failed_write_does_not_stamp_a_mark():
    """写失败不得打 mark，否则下一次写会基于不存在的版本过闸。"""
    files: dict[str, str] = {}
    mw = mw_with_content(files)
    result = mw.wrap_tool_call(
        make_request("write_file", PATH, content="v1"),
        Recorder(result="Error: disk full"),
    )
    assert (result.additional_kwargs or {}).get(READ_MARK_KEY) is None


def test_read_file_still_stamps_marks():
    """既有行为不变：read_file 仍然打 mark。"""
    files = {PATH: "abc"}
    mw = mw_with_content(files)
    req = SimpleNamespace(
        tool_call={"name": "read_file", "args": {"path": PATH}, "id": "c1"},
        state={"messages": []},
        runtime=SimpleNamespace(context={"thread_id": "t1"}, state={}),
    )
    result = mw.wrap_tool_call(req, lambda r: ToolMessage(content="abc", tool_call_id="c1", name="read_file"))
    mark = (result.additional_kwargs or {}).get(READ_MARK_KEY)
    assert isinstance(mark, dict) and mark.get("hash") == hashlib.sha256(b"abc").hexdigest()


# ─────────────────────── B. bash 超长拒绝信息可执行 ───────────────────────


def test_oversized_command_message_reports_actual_and_limit():
    mw = SandboxAuditMiddleware()
    limit = mw._MAX_COMMAND_LENGTH
    reason = mw._validate_input("x" * (limit + 500))
    assert reason is not None
    assert str(limit) in reason, "必须给出上限值"
    assert str(limit + 500) in reason, "必须给出实际长度，模型才知道超了多少"


def test_oversized_command_message_points_to_write_file_and_patches():
    mw = SandboxAuditMiddleware()
    reason = mw._validate_input("y" * (mw._MAX_COMMAND_LENGTH + 1))
    assert "write_file" in reason, "必须指向 write_file（分片 append）"
    assert "apply_json_patches" in reason, "必须指向 apply_json_patches（批量原子编辑）"
    assert "append" in reason


def test_oversized_command_message_names_the_heredoc_antipattern():
    """实测模型正是用 heredoc 写全量产物撞的墙，信息里要点破这一点。"""
    mw = SandboxAuditMiddleware()
    reason = mw._validate_input("z" * (mw._MAX_COMMAND_LENGTH + 1))
    assert "heredoc" in reason.lower()


def test_short_command_still_accepted():
    assert SandboxAuditMiddleware()._validate_input("ls -la") is None


def test_other_rejection_reasons_unchanged():
    mw = SandboxAuditMiddleware()
    assert mw._validate_input("   ") == "empty command"
    assert mw._validate_input("echo \x00") == "null byte detected"
