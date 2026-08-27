"""工具层四类错误的自愈 / 可执行修正（门禁循环治理 Task 15）。

会话 `d393714d` 的四类工具错误各自都能让 agent 白烧一轮或多轮，共同点不是"工具坏了"，而是
**报错文案没告诉它下一步做什么**，于是它换个写法重试同一件事：

| 现象 | 真问题 | 本轮处置 |
|---|---|---|
| `grep` 传了文件路径 → `Path is not a directory` | `grep` 要目录根，而其它文件工具都收文件 | **自愈**：直接搜那一个文件，并说明等价调用形状 |
| `bash` 报 `Unsafe absolute paths: /out` | `$WORKSPACE` 未展开，`$VAR/out` 塌成 `/out` | 命令里有变量时补一句"疑似变量未展开" |
| `glob /mnt/user-data` → `Permission denied` | 该虚拟根是三个目录的并集，无法解析成一个路径 | 点名三个具体根 |
| `skill_manage(action="read")` → "这是内置技能" | 错在 action，却报成名字的问题 | action 先校验；读取意图指向 `read_file` |

⛔ 安全边界不放松：`Unsafe absolute paths` 仍然**拒绝执行**，只是把话说清楚。
"""

from __future__ import annotations

import pytest

pytest.importorskip("deerflow.sandbox.tools")

from deerflow.sandbox import tools as sandbox_tools  # noqa: E402

VIRTUAL = sandbox_tools.VIRTUAL_PATH_PREFIX


# --------------------------------------------------------------------------- #
# ① grep 传文件路径 → 自愈成"搜这一个文件"                                      #
# --------------------------------------------------------------------------- #


class TestGrepGivenAFilePath:
    def _runtime(self):
        return object()

    def test_searches_the_single_file_and_says_so(self, monkeypatch):
        calls: list[tuple] = []

        def fake_grep(runtime, description, pattern, path, glob=None, literal=False, case_sensitive=False, max_results=100):
            calls.append((path, glob))
            return f"Found 1 matches under {path}\n{path}/a.md:3: hit"

        monkeypatch.setattr(sandbox_tools.grep_tool, "func", fake_grep)
        out = sandbox_tools._grep_single_file(
            self._runtime(),
            f"{VIRTUAL}/workspace/a.md",
            "IN-1",
            literal=True,
            case_sensitive=False,
            max_results=100,
        )
        assert calls == [(f"{VIRTUAL}/workspace", "a.md")], "应转成 父目录 + 文件名 glob"
        assert "is a file, not a directory" in out
        assert "hit" in out, "自愈必须真的把结果给出来，而不是只给建议"

    def test_teaches_the_correct_call_shape(self, monkeypatch):
        """自愈但不静默：下次还得让它自己写对。"""
        monkeypatch.setattr(sandbox_tools.grep_tool, "func", lambda *a, **k: "No matches found under x")
        out = sandbox_tools._grep_single_file(self._runtime(), f"{VIRTUAL}/workspace/a.md", "x", literal=False, case_sensitive=False, max_results=10)
        assert f"{VIRTUAL}/workspace" in out and "a.md" in out

    def test_falls_back_to_a_corrective_message_when_the_retry_also_fails(self, monkeypatch):
        monkeypatch.setattr(sandbox_tools.grep_tool, "func", lambda *a, **k: "Error: Directory not found: x")
        out = sandbox_tools._grep_single_file(self._runtime(), f"{VIRTUAL}/workspace/a.md", "x", literal=False, case_sensitive=False, max_results=10)
        assert out.startswith("Error:")
        assert "read_file" in out, "自愈失败时要给出可执行的替代动作"

    def test_bare_filename_without_parent_is_not_healed(self, monkeypatch):
        monkeypatch.setattr(sandbox_tools.grep_tool, "func", lambda *a, **k: pytest.fail("不应重试"))
        out = sandbox_tools._grep_single_file(self._runtime(), "a.md", "x", literal=False, case_sensitive=False, max_results=10)
        assert out == "Error: Path is not a directory: a.md"


# --------------------------------------------------------------------------- #
# ② bash：疑似 shell 变量未展开                                                 #
# --------------------------------------------------------------------------- #


class TestUnexpandedVariableHint:
    def test_hint_when_command_references_a_variable(self):
        hint = sandbox_tools._unexpanded_variable_hint('cd "$WORKSPACE/out" && ls', ["/out"])
        assert "shell variable" in hint
        assert "$VAR/sub" in hint

    def test_braced_variable_is_recognised(self):
        assert sandbox_tools._unexpanded_variable_hint("cat ${OUT}/x.json", ["/x.json"])

    def test_no_hint_when_no_variable_is_referenced(self):
        """明摆着写错的绝对路径不该被扯到变量上去。"""
        assert sandbox_tools._unexpanded_variable_hint("cat /etc/passwd", ["/etc/passwd"]) == ""

    def test_no_hint_for_a_deep_path_that_is_plainly_wrong(self):
        """`$X` 塌陷后剩下的是浅路径；深路径更像是作者真写了那个位置。"""
        assert sandbox_tools._unexpanded_variable_hint("cp $SRC /home/user/data/deep/file", ["/home/user/data/deep/file"]) == ""


# --------------------------------------------------------------------------- #
# ③ 虚拟根不是一个目录                                                          #
# --------------------------------------------------------------------------- #


class TestBareVirtualRoot:
    def _thread_data(self, tmp_path):
        for sub in ("workspace", "uploads", "outputs"):
            (tmp_path / sub).mkdir()
        return {
            "workspace_path": str(tmp_path / "workspace"),
            "uploads_path": str(tmp_path / "uploads"),
            "outputs_path": str(tmp_path / "outputs"),
        }

    def test_error_names_the_three_concrete_roots(self, tmp_path):
        with pytest.raises(PermissionError) as excinfo:
            sandbox_tools.validate_local_tool_path(VIRTUAL, self._thread_data(tmp_path), read_only=True)
        message = str(excinfo.value)
        for sub in ("workspace", "uploads", "outputs"):
            assert f"{VIRTUAL}/{sub}" in message
        assert "union" in message

    def test_trailing_slash_is_the_same_case(self, tmp_path):
        with pytest.raises(PermissionError, match="union"):
            sandbox_tools.validate_local_tool_path(f"{VIRTUAL}/", self._thread_data(tmp_path), read_only=True)

    def test_real_subdirectory_still_passes(self, tmp_path):
        sandbox_tools.validate_local_tool_path(f"{VIRTUAL}/workspace", self._thread_data(tmp_path), read_only=True)

    def test_unrelated_path_keeps_the_generic_message(self, tmp_path):
        with pytest.raises(PermissionError) as excinfo:
            sandbox_tools.validate_local_tool_path("/etc", self._thread_data(tmp_path), read_only=True)
        assert "union" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# ④ skill_manage：错在 action，不要报成名字的问题                                #
# --------------------------------------------------------------------------- #


class TestSkillManageActionValidation:
    @pytest.mark.anyio
    async def test_unsupported_action_names_the_action_not_the_skill(self):
        from deerflow.tools import skill_manage_tool as module

        with pytest.raises(ValueError) as excinfo:
            await module._skill_manage_impl(object(), "read", "eligibility-judgment")
        message = str(excinfo.value)
        assert "Unsupported action 'read'" in message
        assert "built-in skill" not in message, "错在 action 时不得把矛头指向技能名"

    @pytest.mark.anyio
    async def test_read_intent_is_pointed_at_read_file(self):
        from deerflow.tools import skill_manage_tool as module

        with pytest.raises(ValueError, match="read_file"):
            await module._skill_manage_impl(object(), "show", "eligibility-judgment")

    @pytest.mark.anyio
    async def test_valid_actions_are_listed(self):
        from deerflow.tools import skill_manage_tool as module

        with pytest.raises(ValueError) as excinfo:
            await module._skill_manage_impl(object(), "nope", "some-skill")
        for action in ("create", "patch", "edit", "delete", "write_file", "remove_file"):
            assert action in str(excinfo.value)
