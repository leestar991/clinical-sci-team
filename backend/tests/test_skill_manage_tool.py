import importlib
from types import SimpleNamespace

import anyio
import pytest

skill_manage_module = importlib.import_module("deerflow.tools.skill_manage_tool")


def _skill_content(name: str, description: str = "Demo skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"


async def _async_result(decision: str, reason: str):
    from deerflow.skills.security_scanner import ScanResult

    return ScanResult(decision=decision, reason=reason)


def test_skill_manage_create_and_patch(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.skills.security_scanner.get_app_config", lambda: config)
    refresh_calls = []

    async def _refresh():
        refresh_calls.append("refresh")

    monkeypatch.setattr(skill_manage_module, "refresh_skills_system_prompt_cache_async", _refresh)
    monkeypatch.setattr(skill_manage_module, "scan_skill_content", lambda *args, **kwargs: _async_result("allow", "ok"))

    runtime = SimpleNamespace(context={"thread_id": "thread-1"}, config={"configurable": {"thread_id": "thread-1"}})

    result = anyio.run(
        skill_manage_module.skill_manage_tool.coroutine,
        runtime,
        "create",
        "demo-skill",
        _skill_content("demo-skill"),
    )
    assert "Created custom skill" in result

    patch_result = anyio.run(
        skill_manage_module.skill_manage_tool.coroutine,
        runtime,
        "patch",
        "demo-skill",
        None,
        None,
        "Demo skill",
        "Patched skill",
        1,
    )
    # The message names the patched file: patch now targets any file of the skill, so
    # "which file did it touch" is the part worth reading back.
    assert "Patched 'SKILL.md' of custom skill" in patch_result
    assert "Patched skill" in (skills_root / "custom" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert refresh_calls == ["refresh", "refresh"]


def test_skill_manage_patch_replaces_single_occurrence_by_default(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.skills.security_scanner.get_app_config", lambda: config)

    async def _refresh():
        return None

    monkeypatch.setattr(skill_manage_module, "refresh_skills_system_prompt_cache_async", _refresh)
    monkeypatch.setattr(skill_manage_module, "scan_skill_content", lambda *args, **kwargs: _async_result("allow", "ok"))

    runtime = SimpleNamespace(context={"thread_id": "thread-1"}, config={"configurable": {"thread_id": "thread-1"}})
    content = _skill_content("demo-skill", "Demo skill") + "\nRepeated: Demo skill\n"

    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", content)
    patch_result = anyio.run(
        skill_manage_module.skill_manage_tool.coroutine,
        runtime,
        "patch",
        "demo-skill",
        None,
        None,
        "Demo skill",
        "Patched skill",
    )

    skill_text = (skills_root / "custom" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "1 replacement(s) applied, 2 match(es) found" in patch_result
    assert skill_text.count("Patched skill") == 1
    assert skill_text.count("Demo skill") == 1


def test_skill_manage_rejects_public_skill_patch(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    public_dir = skills_root / "public" / "deep-research"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "SKILL.md").write_text(_skill_content("deep-research"), encoding="utf-8")
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)

    runtime = SimpleNamespace(context={}, config={"configurable": {}})

    with pytest.raises(ValueError, match="built-in skill"):
        anyio.run(
            skill_manage_module.skill_manage_tool.coroutine,
            runtime,
            "patch",
            "deep-research",
            None,
            None,
            "Demo skill",
            "Patched",
        )


def test_skill_manage_sync_wrapper_supported(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    refresh_calls = []

    async def _refresh():
        refresh_calls.append("refresh")

    monkeypatch.setattr(skill_manage_module, "refresh_skills_system_prompt_cache_async", _refresh)
    monkeypatch.setattr(skill_manage_module, "scan_skill_content", lambda *args, **kwargs: _async_result("allow", "ok"))

    runtime = SimpleNamespace(context={"thread_id": "thread-sync"}, config={"configurable": {"thread_id": "thread-sync"}})
    result = skill_manage_module.skill_manage_tool.func(
        runtime=runtime,
        action="create",
        name="sync-skill",
        content=_skill_content("sync-skill"),
    )

    assert "Created custom skill" in result
    assert refresh_calls == ["refresh"]


def test_skill_manage_rejects_support_path_traversal(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.skills.security_scanner.get_app_config", lambda: config)

    async def _refresh():
        return None

    monkeypatch.setattr(skill_manage_module, "refresh_skills_system_prompt_cache_async", _refresh)
    monkeypatch.setattr(skill_manage_module, "scan_skill_content", lambda *args, **kwargs: _async_result("allow", "ok"))

    runtime = SimpleNamespace(context={"thread_id": "thread-1"}, config={"configurable": {"thread_id": "thread-1"}})
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill"))

    with pytest.raises(ValueError, match="parent-directory traversal|selected support directory"):
        anyio.run(
            skill_manage_module.skill_manage_tool.coroutine,
            runtime,
            "write_file",
            "demo-skill",
            "malicious overwrite",
            "references/../SKILL.md",
        )


# ── patch 覆盖支撑文件（会话 5aa5d6d6）─────────────────────────────────────────
#
# 在此之前 `patch` 只能改 SKILL.md，脚本的一行修复必须走 `write_file` 全量覆盖。
# 实测代价：主代理修 `criteria_qc_bundle.py` 的两处正则，付了**两次** ~300 行全量重写，
# 第二次只改了 4 个字符（`(?!#)`）。它在推理里明确抱怨过这点，还考虑过绕道 bash 写
# /mnt/skills（正确地否决了——skill_manage 是唯一通道）。


def _fixture(monkeypatch, tmp_path):
    """建好 storage/scanner 的 monkeypatch，返回 (skills_root, runtime, refresh_calls)。"""
    skills_root = tmp_path / "skills"
    config = SimpleNamespace(
        skills=SimpleNamespace(get_skills_path=lambda: skills_root, container_path="/mnt/skills", use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"),
        skill_evolution=SimpleNamespace(enabled=True, moderation_model_name=None),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.skills.security_scanner.get_app_config", lambda: config)
    refresh_calls: list[str] = []

    async def _refresh():
        refresh_calls.append("refresh")

    monkeypatch.setattr(skill_manage_module, "refresh_skills_system_prompt_cache_async", _refresh)
    scan_calls: list[dict] = []

    def _scan(content, *, executable=False, location=""):
        scan_calls.append({"executable": executable, "location": location})
        return _async_result("allow", "ok")

    monkeypatch.setattr(skill_manage_module, "scan_skill_content", _scan)
    runtime = SimpleNamespace(context={"thread_id": "thread-1"}, config={"configurable": {"thread_id": "thread-1"}})
    return skills_root, runtime, refresh_calls, scan_calls


def _make_skill_with_script(runtime, script: str, *, path: str = "scripts/qc.py") -> None:
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill"))
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "write_file", "demo-skill", script, path)


def _patch(runtime, *, find, replace, path=None, expected_count=None, name="demo-skill"):
    return anyio.run(
        skill_manage_module.skill_manage_tool.coroutine,
        runtime,
        "patch",
        name,
        None,  # content
        path,
        find,
        replace,
        expected_count,
    )


_SCRIPT = 'import re\n\nHEADING = re.compile(r"^#{1,3}\\s*\\S")\nOTHER = "keep me"\n'


def test_patch_targets_a_supporting_script(monkeypatch, tmp_path):
    """一行修复 = 一对 find/replace，不必重发整份脚本。"""
    skills_root, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)

    result = _patch(runtime, find=r'r"^#{1,3}\s*\S"', replace=r'r"^#{1,3}(?!#)\s*\S"', path="scripts/qc.py")

    text = (skills_root / "custom" / "demo-skill" / "scripts" / "qc.py").read_text(encoding="utf-8")
    assert "Patched 'scripts/qc.py' of custom skill 'demo-skill'" in result
    assert "(?!#)" in text
    assert 'OTHER = "keep me"' in text, "patch 不得动到目标以外的内容"


def test_patch_without_path_still_targets_skill_md(monkeypatch, tmp_path):
    """向后兼容：不给 path 就是老行为。"""
    skills_root, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill", "Demo skill"))

    result = _patch(runtime, find="Demo skill", replace="Patched skill")

    assert "Patched 'SKILL.md'" in result
    assert "Patched skill" in (skills_root / "custom" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")


def test_patch_with_explicit_skill_md_path_is_treated_as_skill_md(monkeypatch, tmp_path):
    """显式传 path="SKILL.md" 与省略等价（否则会去 ensure_safe_support_path 而被拒）。"""
    skills_root, runtime, refresh_calls, _ = _fixture(monkeypatch, tmp_path)
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill", "Demo skill"))

    result = _patch(runtime, find="Demo skill", replace="Patched skill", path="SKILL.md")

    assert "Patched 'SKILL.md'" in result
    # create + patch 各刷一次系统提示缓存
    assert refresh_calls == ["refresh", "refresh"]


def test_patch_on_a_script_does_not_refresh_the_prompt_cache(monkeypatch, tmp_path):
    """只有 SKILL.md 正文进系统提示；为脚本改动重建缓存是白付。"""
    _, runtime, refresh_calls, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)
    refresh_calls.clear()

    _patch(runtime, find='OTHER = "keep me"', replace='OTHER = "changed"', path="scripts/qc.py")

    assert refresh_calls == []


def test_patch_scans_scripts_as_executable(monkeypatch, tmp_path):
    """脚本必须走可执行内容的严格扫描 —— 不能因为换了 action 就被当散文放过。"""
    _, runtime, _, scan_calls = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)
    scan_calls.clear()

    _patch(runtime, find='OTHER = "keep me"', replace='OTHER = "changed"', path="scripts/qc.py")

    assert scan_calls == [{"executable": True, "location": "demo-skill/scripts/qc.py"}]


def test_patch_scans_references_as_prose(monkeypatch, tmp_path):
    _, runtime, _, scan_calls = _fixture(monkeypatch, tmp_path)
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill"))
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "write_file", "demo-skill", "old text\n", "references/notes.md")
    scan_calls.clear()

    _patch(runtime, find="old text", replace="new text", path="references/notes.md")

    assert scan_calls == [{"executable": False, "location": "demo-skill/references/notes.md"}]


def test_patch_refuses_an_ambiguous_find_on_a_supporting_file(monkeypatch, tmp_path):
    """多处匹配又不给 expected_count → 拒绝。

    静默只改第一处会留下一份**仍然能解析**的半修文件，这正是最难发现的一类损坏。
    """
    skills_root, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, 'A = "dup"\nB = "dup"\n')

    with pytest.raises(ValueError, match="appears 2 times"):
        _patch(runtime, find='"dup"', replace='"fixed"', path="scripts/qc.py")

    # 拒绝时文件不得被动过
    assert (skills_root / "custom" / "demo-skill" / "scripts" / "qc.py").read_text(encoding="utf-8") == 'A = "dup"\nB = "dup"\n'


def test_patch_replaces_all_matches_when_expected_count_is_given(monkeypatch, tmp_path):
    skills_root, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, 'A = "dup"\nB = "dup"\n')

    result = _patch(runtime, find='"dup"', replace='"fixed"', path="scripts/qc.py", expected_count=2)

    text = (skills_root / "custom" / "demo-skill" / "scripts" / "qc.py").read_text(encoding="utf-8")
    assert "2 replacement(s) applied, 2 match(es) found" in result
    assert text.count('"fixed"') == 2 and '"dup"' not in text


def test_patch_ambiguity_guard_does_not_apply_to_skill_md(monkeypatch, tmp_path):
    """SKILL.md 保持历史默认（改第一处）—— 已有的散文 patch 是按这个语义写的。"""
    skills_root, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    content = _skill_content("demo-skill", "Demo skill") + "\nRepeated: Demo skill\n"
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", content)

    result = _patch(runtime, find="Demo skill", replace="Patched skill")

    assert "1 replacement(s) applied, 2 match(es) found" in result
    assert (skills_root / "custom" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8").count("Demo skill") == 1


def test_patch_expected_count_mismatch_names_the_file(monkeypatch, tmp_path):
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, 'A = "dup"\nB = "dup"\n')

    with pytest.raises(ValueError, match=r"found 2 in scripts/qc\.py"):
        _patch(runtime, find='"dup"', replace='"x"', path="scripts/qc.py", expected_count=3)


def test_patch_missing_target_text_names_the_file(monkeypatch, tmp_path):
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)

    with pytest.raises(ValueError, match=r"not found in scripts/qc\.py"):
        _patch(runtime, find="nothing like this", replace="x", path="scripts/qc.py")


def test_patch_on_a_missing_supporting_file_points_at_write_file(monkeypatch, tmp_path):
    """patch 不创建文件 —— 报错要说清该用哪个 action。"""
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    anyio.run(skill_manage_module.skill_manage_tool.coroutine, runtime, "create", "demo-skill", _skill_content("demo-skill"))

    with pytest.raises(FileNotFoundError, match="Use write_file to create it"):
        _patch(runtime, find="x", replace="y", path="scripts/absent.py")


def test_patch_rejects_path_traversal(monkeypatch, tmp_path):
    """路径守卫与 write_file 同源（ensure_safe_support_path），patch 不得绕过它。"""
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)

    with pytest.raises(ValueError):
        _patch(runtime, find="x", replace="y", path="../../etc/passwd")
    with pytest.raises(ValueError):
        _patch(runtime, find="x", replace="y", path="secrets.py")


def test_patch_records_the_supporting_file_in_history(monkeypatch, tmp_path):
    """历史必须记到具体文件、且带前后内容，否则回滚不知道该还原哪一份。"""
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)
    _make_skill_with_script(runtime, _SCRIPT)
    _patch(runtime, find='OTHER = "keep me"', replace='OTHER = "changed"', path="scripts/qc.py")

    from deerflow.skills.storage import get_or_new_skill_storage

    records = get_or_new_skill_storage().read_history("demo-skill")
    patches = [r for r in records if r.get("action") == "patch"]
    assert len(patches) == 1, records
    assert patches[0]["file_path"] == "scripts/qc.py"
    assert 'OTHER = "keep me"' in patches[0]["prev_content"]
    assert 'OTHER = "changed"' in patches[0]["new_content"]


def test_patch_rejects_public_skill_supporting_file(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    public_dir = skills_root / "public" / "deep-research"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "SKILL.md").write_text(_skill_content("deep-research"), encoding="utf-8")
    (public_dir / "scripts").mkdir(exist_ok=True)
    (public_dir / "scripts" / "g.py").write_text("X = 1\n", encoding="utf-8")
    _, runtime, _, _ = _fixture(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="built-in skill"):
        _patch(runtime, find="X = 1", replace="X = 2", path="scripts/g.py", name="deep-research")
    assert (public_dir / "scripts" / "g.py").read_text(encoding="utf-8") == "X = 1\n"


def test_tool_description_advertises_patching_supporting_files():
    """工具描述是 agent 唯一的说明书 —— 它写「patch 只能改 SKILL.md」时，agent 就只会全量重写。

    会话 `5aa5d6d6`：主代理修 `criteria_qc_bundle.py` 的两处正则，两次都走 `write_file` 全量
    覆盖 ~300 行（第二次只改 4 个字符）。它在推理里明确读到了「patch is a find/replace inside
    SKILL.md only」并据此排除了 patch —— 描述与能力一致，才不会让人白付。
    """
    description = skill_manage_module.skill_manage_tool.description
    assert "SKILL.md only" not in description, "描述仍声称 patch 只能改 SKILL.md"
    assert "Prefer patch over write_file" in description, "未引导小改动优先用 patch"
    assert "path" in description and "patch" in description
    # 歧义守卫必须写进描述：agent 不知道它存在，就会以为 patch 会静默改第一处
    assert "ambiguous" in description.lower(), "未说明 patch 会拒绝歧义的 find"


def test_docstring_and_impl_agree_on_which_actions_take_path():
    """`path` 现在服务三个 action；描述漏掉任一个都会让 agent 少一条路。"""
    description = skill_manage_module.skill_manage_tool.description
    for action in ("write_file", "remove_file", "patch"):
        assert action in description, f"描述未提到 {action}"
