"""Tool for creating and evolving custom skills."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from weakref import WeakValueDictionary

from langchain.tools import tool

from deerflow.agents.lead_agent.prompt import refresh_skills_system_prompt_cache_async
from deerflow.skills.security_scanner import scan_skill_content
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.skills.types import SKILL_MD_FILE
from deerflow.tools.sync import make_sync_tool_wrapper
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

_skill_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

# Kept next to the dispatch chain below; an action missing here is rejected up front rather
# than falling through to a message about the skill name.
_VALID_ACTIONS = frozenset({"create", "patch", "edit", "delete", "write_file", "remove_file"})


def _get_lock(name: str) -> asyncio.Lock:
    lock = _skill_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _skill_locks[name] = lock
    return lock


def _get_thread_id(runtime: Runtime | None) -> str | None:
    if runtime is None:
        return None
    if runtime.context and runtime.context.get("thread_id"):
        return runtime.context.get("thread_id")
    return runtime.config.get("configurable", {}).get("thread_id")


def _history_record(*, action: str, file_path: str, prev_content: str | None, new_content: str | None, thread_id: str | None, scanner: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "author": "agent",
        "thread_id": thread_id,
        "file_path": file_path,
        "prev_content": prev_content,
        "new_content": new_content,
        "scanner": scanner,
    }


def _is_executable_support_path(path: str) -> bool:
    """Whether a supporting-file path holds code, i.e. needs the stricter scan.

    Shared by ``write_file`` and ``patch`` so a script cannot be scanned as prose by
    arriving through the other action.
    """
    return path.startswith("scripts/") or "/scripts/" in path


async def _scan_or_raise(content: str, *, executable: bool, location: str) -> dict[str, str]:
    result = await scan_skill_content(content, executable=executable, location=location)
    if result.decision == "block":
        raise ValueError(f"Security scan blocked the write: {result.reason}")
    if executable and result.decision != "allow":
        raise ValueError(f"Security scan rejected executable content: {result.reason}")
    return {"decision": result.decision, "reason": result.reason}


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def _skill_manage_impl(
    runtime: Runtime,
    action: str,
    name: str,
    content: str | None = None,
    path: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    expected_count: int | None = None,
) -> str:
    """Manage custom skills under skills/custom/.

    Args:
        action: One of create, patch, edit, delete, write_file, remove_file.
        name: Skill name in hyphen-case.
        content: New file content for create, edit, or write_file.
        path: Supporting file path for write_file, remove_file, or patch (patch defaults to SKILL.md when omitted).
        find: Existing text to replace for patch.
        replace: Replacement text for patch.
        expected_count: Optional expected number of replacements for patch.
    """
    name = SkillStorage.validate_skill_name(name)
    if action not in _VALID_ACTIONS:
        # Checked BEFORE anything else on purpose. The fall-through at the end of this
        # function reports "'{name}' is a built-in skill, create a custom one instead"
        # whenever the name happens to be a public skill — so a bad *action* on a built-in
        # name used to be reported as a problem with the name. An agent that only wanted to
        # read a skill then went off to create one. Name the real fault, and point read
        # intent at the tool that serves it.
        hint = ""
        if action in {"read", "get", "show", "view", "list", "cat"}:
            hint = " This tool only writes; use read_file on the skill's SKILL.md to read it."
        raise ValueError(f"Unsupported action '{action}'. Valid actions: {', '.join(sorted(_VALID_ACTIONS))}.{hint}")
    lock = _get_lock(name)
    thread_id = _get_thread_id(runtime)
    skill_storage = get_or_new_skill_storage()

    async with lock:
        if action == "create":
            if await _to_thread(skill_storage.custom_skill_exists, name):
                raise ValueError(f"Custom skill '{name}' already exists.")
            if content is None:
                raise ValueError("content is required for create.")
            await _to_thread(skill_storage.validate_skill_markdown_content, name, content)
            scan = await _scan_or_raise(content, executable=False, location=f"{name}/{SKILL_MD_FILE}")
            await _to_thread(skill_storage.write_custom_skill, name, SKILL_MD_FILE, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="create", file_path=SKILL_MD_FILE, prev_content=None, new_content=content, thread_id=thread_id, scanner=scan),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Created custom skill '{name}'."

        if action == "edit":
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if content is None:
                raise ValueError("content is required for edit.")
            await _to_thread(skill_storage.validate_skill_markdown_content, name, content)
            scan = await _scan_or_raise(content, executable=False, location=f"{name}/{SKILL_MD_FILE}")
            skill_file = skill_storage.get_custom_skill_file(name)
            prev_content = await _to_thread(skill_file.read_text, encoding="utf-8")
            await _to_thread(skill_storage.write_custom_skill, name, SKILL_MD_FILE, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="edit", file_path=SKILL_MD_FILE, prev_content=prev_content, new_content=content, thread_id=thread_id, scanner=scan),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Updated custom skill '{name}'."

        if action == "patch":
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if find is None or replace is None:
                raise ValueError("find and replace are required for patch.")
            # `path` selects a supporting file; omitting it keeps the historical SKILL.md
            # behaviour. Before this, a one-character fix in a skill's helper script had to
            # go through `write_file` with the WHOLE file re-emitted — see the docstring.
            is_skill_md = path is None or path == SKILL_MD_FILE
            if is_skill_md:
                target = skill_storage.get_custom_skill_file(name)
                target_path = SKILL_MD_FILE
            else:
                target = await _to_thread(skill_storage.ensure_safe_support_path, name, path)
                target_path = path
                if not await _to_thread(target.exists):
                    raise FileNotFoundError(f"Supporting file '{path}' not found for skill '{name}'. Use write_file to create it.")
            prev_content = await _to_thread(target.read_text, encoding="utf-8")
            occurrences = prev_content.count(find)
            if occurrences == 0:
                raise ValueError(f"Patch target not found in {target_path}.")
            if expected_count is not None and occurrences != expected_count:
                raise ValueError(f"Expected {expected_count} replacements but found {occurrences} in {target_path}.")
            # Ambiguity is a silent-corruption risk on scripts: replacing only the first of
            # several identical snippets leaves the file syntactically valid and half-fixed.
            # SKILL.md keeps the historical "replace the first match" default because prose
            # patches were written against it; supporting files must be unambiguous.
            if not is_skill_md and expected_count is None and occurrences > 1:
                raise ValueError(
                    f"'{find[:60]}...' appears {occurrences} times in {target_path}. "
                    "Pass expected_count to replace them all, or extend `find` with surrounding "
                    "context so it matches exactly once — patching only the first of several "
                    "identical snippets leaves a half-fixed file that still parses."
                )
            replacement_count = expected_count if expected_count is not None else 1
            new_content = prev_content.replace(find, replace, replacement_count)
            executable = _is_executable_support_path(target_path)
            if is_skill_md:
                await _to_thread(skill_storage.validate_skill_markdown_content, name, new_content)
            scan = await _scan_or_raise(new_content, executable=executable, location=f"{name}/{target_path}")
            await _to_thread(skill_storage.write_custom_skill, name, target_path, new_content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="patch", file_path=target_path, prev_content=prev_content, new_content=new_content, thread_id=thread_id, scanner=scan),
            )
            # Only SKILL.md text reaches the system prompt; refreshing for a script edit
            # would cost a cache rebuild for nothing.
            if is_skill_md:
                await refresh_skills_system_prompt_cache_async()
            return f"Patched '{target_path}' of custom skill '{name}' ({replacement_count} replacement(s) applied, {occurrences} match(es) found)."

        if action == "delete":
            await _to_thread(
                skill_storage.delete_custom_skill,
                name,
                history_meta=_history_record(
                    action="delete",
                    file_path=SKILL_MD_FILE,
                    prev_content=None,
                    new_content=None,
                    thread_id=thread_id,
                    scanner={"decision": "allow", "reason": "Deletion requested."},
                ),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Deleted custom skill '{name}'."

        if action == "write_file":
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if path is None or content is None:
                raise ValueError("path and content are required for write_file.")
            target = await _to_thread(skill_storage.ensure_safe_support_path, name, path)
            exists = await _to_thread(target.exists)
            prev_content = await _to_thread(target.read_text, encoding="utf-8") if exists else None
            scan = await _scan_or_raise(content, executable=_is_executable_support_path(path), location=f"{name}/{path}")
            await _to_thread(skill_storage.write_custom_skill, name, path, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="write_file", file_path=path, prev_content=prev_content, new_content=content, thread_id=thread_id, scanner=scan),
            )
            return f"Wrote '{path}' for custom skill '{name}'."

        if action == "remove_file":
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if path is None:
                raise ValueError("path is required for remove_file.")
            target = await _to_thread(skill_storage.ensure_safe_support_path, name, path)
            if not await _to_thread(target.exists):
                raise FileNotFoundError(f"Supporting file '{path}' not found for skill '{name}'.")
            prev_content = await _to_thread(target.read_text, encoding="utf-8")
            await _to_thread(target.unlink)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="remove_file", file_path=path, prev_content=prev_content, new_content=None, thread_id=thread_id, scanner={"decision": "allow", "reason": "Deletion requested."}),
            )
            return f"Removed '{path}' from custom skill '{name}'."

        if await _to_thread(skill_storage.public_skill_exists, name):
            raise ValueError(f"'{name}' is a built-in skill. To customise it, create a new skill with the same name under skills/custom/.")
        raise ValueError(f"Unsupported action '{action}'. Valid actions: {', '.join(sorted(_VALID_ACTIONS))}.")


@tool("skill_manage", parse_docstring=True)
async def skill_manage_tool(
    runtime: Runtime,
    action: str,
    name: str,
    content: str | None = None,
    path: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    expected_count: int | None = None,
) -> str:
    """Create and edit custom skills under skills/custom/ — including their scripts.

    This is the ONLY way to write anything under /mnt/skills: the file tools (write_file,
    str_replace, apply_json_patches) all reject that path as read-only. Reach for this tool
    whenever a skill's own files need changing, not just its SKILL.md.

    write_file covers EVERY supporting file of a custom skill — scripts/*.py,
    references/*.md, assets/* — creating or overwriting the whole file at `path` (relative
    to the skill root). So a bug in a skill's helper script is fixable here: read it with
    read_file, then write the corrected version back with action="write_file",
    path="scripts/<name>.py". Executable content is security-scanned and every edit is
    recorded in the skill's history.

    patch is a find/replace and works on ANY file of the skill: SKILL.md by default, or the
    supporting file named by `path`. **Prefer patch over write_file for a small fix** — a
    one-line change to a script costs one `find`/`replace` pair instead of re-emitting the
    whole file. (Real cost of not having it: fixing two regexes in a ~300-line skill script
    took two full-file rewrites, and the second one was a 4-character change.) write_file
    remains the way to create a file or rewrite most of it; edit replaces SKILL.md wholesale.

    On supporting files, patch refuses an ambiguous `find` (several matches, no
    expected_count) rather than silently patching the first one — a half-fixed script still
    parses. Pass expected_count to replace every match, or extend `find` with surrounding
    context. Scripts are security-scanned as executable content either way.

    remove_file deletes a supporting file at `path`; delete removes the whole skill.

    Built-in (public) skills cannot be modified by any action. To change one, create a
    custom skill of the same name.

    Args:
        action: One of create, patch, edit, delete, write_file, remove_file.
        name: Skill name in hyphen-case.
        content: New file content for create, edit, or write_file.
        path: Supporting file path, relative to the skill root (e.g. scripts/qc.py). Required for write_file and remove_file; optional for patch (omit to patch SKILL.md).
        find: Existing text to replace for patch.
        replace: Replacement text for patch.
        expected_count: Optional expected number of replacements for patch. Required when `find` matches more than once in a supporting file.
    """
    return await _skill_manage_impl(
        runtime=runtime,
        action=action,
        name=name,
        content=content,
        path=path,
        find=find,
        replace=replace,
        expected_count=expected_count,
    )


skill_manage_tool.func = make_sync_tool_wrapper(_skill_manage_impl, "skill_manage")
