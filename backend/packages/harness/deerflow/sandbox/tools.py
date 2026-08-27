import asyncio
import json
import os
import posixpath
import re
import shlex
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain.tools import tool

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.runtime.secret_context import read_active_secrets
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from deerflow.sandbox.file_operation_lock import get_file_operation_lock
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.sandbox.search import GrepMatch
from deerflow.sandbox.security import LOCAL_HOST_BASH_DISABLED_MESSAGE, is_host_bash_allowed
from deerflow.tools.types import Runtime

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![:\w])(?<!:/)/(?:[^\s\"'`;&|<>()]+)")
# A ``{...}`` block holding a single identifier-like placeholder (e.g. ``{id}``
# in a REST template or ``{port}`` in an f-string). Bash brace expansion such as
# ``{passwd,shadow}`` or ``{,.bak}`` does NOT match (commas/dots/empty inner).
_IDENTIFIER_BRACE_BLOCK_PATTERN = re.compile(r"\{([^{}]*)\}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE_URL_PATTERN = re.compile(r"\bfile://\S+", re.IGNORECASE)
# `$VAR` / `${VAR}` — used only to decide whether an "unsafe absolute path" rejection is
# worth explaining as a possible unexpanded variable.
_SHELL_VARIABLE_PATTERN = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_URL_WITH_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_URL_IN_COMMAND_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'`;&|<>()]+", re.IGNORECASE)
_DOTDOT_PATH_SEGMENT_PATTERN = re.compile(r"(?:^|[/\\=])\.\.(?:$|[/\\])")
_LOCAL_BASH_SYSTEM_PATH_PREFIXES = (
    "/bin/",
    "/usr/bin/",
    "/usr/sbin/",
    "/sbin/",
    "/opt/homebrew/bin/",
    "/dev/",
)

_DEFAULT_SKILLS_CONTAINER_PATH = "/mnt/skills"
_ACP_WORKSPACE_VIRTUAL_PATH = "/mnt/acp-workspace"
_DEFAULT_GLOB_MAX_RESULTS = 200
_MAX_GLOB_MAX_RESULTS = 1000
_DEFAULT_GREP_MAX_RESULTS = 100
_MAX_GREP_MAX_RESULTS = 500
_DEFAULT_WRITE_FILE_ERROR_MAX_CHARS = 2000

# Maximum bytes accepted in a single non-append write_file call (issue #3189).
# Oversized single-shot writes correlate with LLM streaming chunk-gap timeouts
# because the tool-call JSON payload (which the model must emit as one
# continuous stream) grows past the safe window. 80 KB ≈ 20K tokens, a
# comfortable headroom under the factory-default 240s stream_chunk_timeout.
# Deployments can override via env var DEERFLOW_WRITE_FILE_MAX_BYTES; set to
# 0 (or negative) to disable the guard entirely.
_WRITE_FILE_CONTENT_MAX_BYTES = 80 * 1024
_WRITE_FILE_MAX_BYTES_ENV = "DEERFLOW_WRITE_FILE_MAX_BYTES"
_LOCAL_BASH_CWD_COMMANDS = {"cd", "pushd"}
_LOCAL_BASH_COMMAND_WRAPPERS = {"command", "builtin"}
_LOCAL_BASH_COMMAND_PREFIX_KEYWORDS = {"!", "{", "case", "do", "elif", "else", "for", "if", "select", "then", "time", "until", "while"}
_LOCAL_BASH_COMMAND_END_KEYWORDS = {"}", "done", "esac", "fi"}
_LOCAL_BASH_ROOT_PATH_COMMANDS = {
    "awk",
    "cat",
    "cp",
    "du",
    "find",
    "grep",
    "head",
    "less",
    "ln",
    "ls",
    "more",
    "mv",
    "rm",
    "sed",
    "tail",
    "tar",
}
_SHELL_COMMAND_SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")"}
_SHELL_REDIRECTION_OPERATORS = {
    "<",
    ">",
    "<<",
    ">>",
    "<<<",
    "<>",
    ">&",
    "<&",
    "&>",
    "&>>",
    ">|",
}


def _get_skills_container_path() -> str:
    """Get the skills container path from config, with fallback to default.

    Result is cached after the first successful config load.  If config loading
    fails the default is returned *without* caching so that a later call can
    pick up the real value once the config is available.
    """
    cached = getattr(_get_skills_container_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        from deerflow.config import get_app_config

        value = get_app_config().skills.container_path
        _get_skills_container_path._cached = value  # type: ignore[attr-defined]
        return value
    except Exception:
        return _DEFAULT_SKILLS_CONTAINER_PATH


def _get_skills_host_path() -> str | None:
    """Get the skills host filesystem path from config.

    Returns None if the skills directory does not exist or config cannot be
    loaded.  Only successful lookups are cached; failures are retried on the
    next call so that a transiently unavailable skills directory does not
    permanently disable skills access.
    """
    cached = getattr(_get_skills_host_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        from deerflow.config import get_app_config

        config = get_app_config()
        skills_path = config.skills.get_skills_path()
        if skills_path.exists():
            value = str(skills_path)
            _get_skills_host_path._cached = value  # type: ignore[attr-defined]
            return value
    except Exception:
        pass
    return None


def _is_skills_path(path: str) -> bool:
    """Check if a path is under the skills container path."""
    skills_prefix = _get_skills_container_path()
    return path == skills_prefix or path.startswith(f"{skills_prefix}/")


def _skills_write_hint(path: str) -> str:
    """Point a rejected skills write at ``skill_manage``, when the skill is writable.

    Session ``a7c19ea1``: the agent found a real bug in
    ``/mnt/skills/custom/criteria-parser/scripts/criteria_qc_bundle.py`` and tried twice to
    patch it with ``apply_json_patches``, getting only "Permission denied" both times. It
    then reported the fix as blocked — while ``skill_manage(action="write_file")`` could
    have written that exact file all along (it accepts any support path under the skill,
    ``scripts/`` included, behind a security scan). The agent's own words were that
    ``skill_manage patch`` "只作用于 SKILL.md", so the capability existed and was invisible.

    Only ``custom/`` skills get the pointer: ``public/`` ones are genuinely read-only, and
    sending an agent to create a custom copy of a built-in skill is its own failure mode.
    """
    prefix = f"{_get_skills_container_path()}/custom/"
    if not path.startswith(prefix):
        return " Built-in (public) skills are read-only."
    remainder = path[len(prefix) :].strip("/")
    if not remainder:
        return ""
    skill_name, _, support_path = remainder.partition("/")
    target = support_path or "SKILL.md"
    return (
        f" Custom skills are writable through the skill_manage tool, not the file tools: "
        f'skill_manage(action="write_file", name="{skill_name}", path="{target}", content=...) '
        '— it applies a security scan and records an edit history. Use action="patch" for a '
        "find/replace inside SKILL.md."
    )


def _resolve_skills_path(path: str) -> str:
    """Resolve a virtual skills path to a host filesystem path.

    Args:
        path: Virtual skills path (e.g. /mnt/skills/public/bootstrap/SKILL.md)

    Returns:
        Resolved host path.

    Raises:
        FileNotFoundError: If skills directory is not configured or doesn't exist.
    """
    skills_container = _get_skills_container_path()
    skills_host = _get_skills_host_path()
    if skills_host is None:
        raise FileNotFoundError(f"Skills directory not available for path: {path}")

    if path == skills_container:
        return skills_host

    relative = path[len(skills_container) :].lstrip("/")
    return _join_path_preserving_style(skills_host, relative)


def _is_acp_workspace_path(path: str) -> bool:
    """Check if a path is under the ACP workspace virtual path."""
    return path == _ACP_WORKSPACE_VIRTUAL_PATH or path.startswith(f"{_ACP_WORKSPACE_VIRTUAL_PATH}/")


def _get_custom_mounts():
    """Get custom volume mounts from sandbox config.

    Result is cached after the first successful config load.  If config loading
    fails an empty list is returned *without* caching so that a later call can
    pick up the real value once the config is available.
    """
    cached = getattr(_get_custom_mounts, "_cached", None)
    if cached is not None:
        return cached
    try:
        from pathlib import Path

        from deerflow.config import get_app_config

        config = get_app_config()
        mounts = []
        if config.sandbox and config.sandbox.mounts:
            # Only include mounts whose host_path exists, consistent with
            # LocalSandboxProvider._setup_path_mappings() which also filters
            # by host_path.exists().
            mounts = [m for m in config.sandbox.mounts if Path(m.host_path).exists()]
        _get_custom_mounts._cached = mounts  # type: ignore[attr-defined]
        return mounts
    except Exception:
        # If config loading fails, return an empty list without caching so that
        # a later call can retry once the config is available.
        return []


def _is_custom_mount_path(path: str) -> bool:
    """Check if path is under a custom mount container_path."""
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            return True
    return False


def _get_custom_mount_for_path(path: str):
    """Get the mount config matching this path (longest prefix first)."""
    best = None
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            if best is None or len(mount.container_path) > len(best.container_path):
                best = mount
    return best


def _extract_thread_id_from_thread_data(thread_data: "ThreadDataState | None") -> str | None:
    """Extract thread_id from thread_data by inspecting workspace_path.

    The workspace_path has the form
    ``{base_dir}/threads/{thread_id}/user-data/workspace``, so
    ``Path(workspace_path).parent.parent.name`` yields the thread_id.
    """
    if thread_data is None:
        return None
    workspace_path = thread_data.get("workspace_path")
    if not workspace_path:
        return None
    try:
        # {base_dir}/threads/{thread_id}/user-data/workspace → parent.parent = threads/{thread_id}
        return Path(workspace_path).parent.parent.name
    except Exception:
        return None


def _get_acp_workspace_host_path(thread_id: str | None = None) -> str | None:
    """Get the ACP workspace host filesystem path.

    When *thread_id* is provided, returns the per-thread workspace
    ``{base_dir}/threads/{thread_id}/acp-workspace/`` (not cached — the
    directory is created on demand by ``invoke_acp_agent_tool``).

    Falls back to the global ``{base_dir}/acp-workspace/`` when *thread_id*
    is ``None``; that result is cached after the first successful resolution.
    Returns ``None`` if the directory does not exist.
    """
    if thread_id is not None:
        try:
            from deerflow.config.paths import get_paths
            from deerflow.runtime.user_context import get_effective_user_id

            host_path = get_paths().acp_workspace_dir(thread_id, user_id=get_effective_user_id())
            if host_path.exists():
                return str(host_path)
        except Exception:
            pass
        return None

    cached = getattr(_get_acp_workspace_host_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        from deerflow.config.paths import get_paths

        host_path = get_paths().base_dir / "acp-workspace"
        if host_path.exists():
            value = str(host_path)
            _get_acp_workspace_host_path._cached = value  # type: ignore[attr-defined]
            return value
    except Exception:
        pass
    return None


def _resolve_acp_workspace_path(path: str, thread_id: str | None = None) -> str:
    """Resolve a virtual ACP workspace path to a host filesystem path.

    Args:
        path: Virtual path (e.g. /mnt/acp-workspace/hello_world.py)
        thread_id: Current thread ID for per-thread workspace resolution.
                   When ``None``, falls back to the global workspace.

    Returns:
        Resolved host path.

    Raises:
        FileNotFoundError: If ACP workspace directory does not exist.
        PermissionError: If path traversal is detected.
    """
    _reject_path_traversal(path)

    host_path = _get_acp_workspace_host_path(thread_id)
    if host_path is None:
        raise FileNotFoundError(f"ACP workspace directory not available for path: {path}")

    if path == _ACP_WORKSPACE_VIRTUAL_PATH:
        return host_path

    relative = path[len(_ACP_WORKSPACE_VIRTUAL_PATH) :].lstrip("/")
    resolved = _join_path_preserving_style(host_path, relative)

    if "/" in host_path and "\\" not in host_path:
        base_path = posixpath.normpath(host_path)
        candidate_path = posixpath.normpath(resolved)
        try:
            if posixpath.commonpath([base_path, candidate_path]) != base_path:
                raise PermissionError("Access denied: path traversal detected")
        except ValueError:
            raise PermissionError("Access denied: path traversal detected") from None
        return resolved

    resolved_path = Path(resolved).resolve()
    try:
        resolved_path.relative_to(Path(host_path).resolve())
    except ValueError:
        raise PermissionError("Access denied: path traversal detected")

    return str(resolved_path)


def _get_mcp_allowed_paths() -> list[str]:
    """Get the list of allowed paths from MCP config for file system server."""
    allowed_paths = []
    try:
        from deerflow.config.extensions_config import get_extensions_config

        extensions_config = get_extensions_config()

        for _, server in extensions_config.mcp_servers.items():
            if not server.enabled:
                continue

            # Only check the filesystem server
            args = server.args or []
            # Check if args has server-filesystem package
            has_filesystem = any("server-filesystem" in arg for arg in args)
            if not has_filesystem:
                continue
            # Unpack the allowed file system paths in config
            for arg in args:
                if not arg.startswith("-") and arg.startswith("/"):
                    allowed_paths.append(arg.rstrip("/") + "/")

    except Exception:
        pass

    return allowed_paths


def _get_tool_config_int(name: str, key: str, default: int) -> int:
    try:
        tool_config = get_app_config().get_tool_config(name)
        if tool_config is not None and key in tool_config.model_extra:
            value = tool_config.model_extra.get(key)
            if isinstance(value, int):
                return value
    except Exception:
        pass
    return default


def _clamp_max_results(value: int, *, default: int, upper_bound: int) -> int:
    if value <= 0:
        return default
    return min(value, upper_bound)


def _resolve_max_results(name: str, requested: int, *, default: int, upper_bound: int) -> int:
    requested_max_results = _clamp_max_results(requested, default=default, upper_bound=upper_bound)
    configured_max_results = _clamp_max_results(
        _get_tool_config_int(name, "max_results", default),
        default=default,
        upper_bound=upper_bound,
    )
    return min(requested_max_results, configured_max_results)


def _resolve_local_read_path(path: str, thread_data: ThreadDataState) -> str:
    validate_local_tool_path(path, thread_data, read_only=True)
    if _is_skills_path(path):
        return _resolve_skills_path(path)
    if _is_acp_workspace_path(path):
        return _resolve_acp_workspace_path(path, _extract_thread_id_from_thread_data(thread_data))
    return _resolve_and_validate_user_data_path(path, thread_data)


def _format_glob_results(root_path: str, matches: list[str], truncated: bool) -> str:
    if not matches:
        return f"No files matched under {root_path}"

    lines = [f"Found {len(matches)} paths under {root_path}"]
    if truncated:
        lines[0] += f" (showing first {len(matches)})"
    lines.extend(f"{index}. {path}" for index, path in enumerate(matches, start=1))
    if truncated:
        lines.append("Results truncated. Narrow the path or pattern to see fewer matches.")
    return "\n".join(lines)


def _grep_single_file(
    runtime: Runtime,
    file_path: str,
    pattern: str,
    *,
    literal: bool,
    case_sensitive: bool,
    max_results: int,
) -> str:
    """Run a ``grep`` whose ``path`` turned out to be a file, not a directory.

    Implemented by searching the file's parent with a glob pinned to its name, so it reuses
    the same sandbox ``grep`` path (and the same masking and truncation) instead of adding a
    second content-matching implementation that could drift from it.

    The note in front of the results is the point: a silent fix teaches nothing and the next
    call repeats the mistake.
    """
    parent, _, filename = file_path.replace("\\", "/").rpartition("/")
    if not parent or not filename:
        return f"Error: Path is not a directory: {file_path}"
    healed = grep_tool.func(
        runtime,
        f"grep inside the single file {filename}",
        pattern,
        parent,
        filename,
        literal,
        case_sensitive,
        max_results,
    )
    if isinstance(healed, str) and healed.startswith("Error:"):
        return f"Error: Path is not a directory: {file_path}. `grep` takes a directory root — call it with path={parent!r} and glob={filename!r}, or use read_file for one file."
    return f"[grep] path={file_path} is a file, not a directory; searched that one file (equivalent to path={parent!r}, glob={filename!r}).\n{healed}"


def _format_grep_results(root_path: str, matches: list[GrepMatch], truncated: bool) -> str:
    if not matches:
        return f"No matches found under {root_path}"

    lines = [f"Found {len(matches)} matches under {root_path}"]
    if truncated:
        lines[0] += f" (showing first {len(matches)})"
    lines.extend(f"{match.path}:{match.line_number}: {match.line}" for match in matches)
    if truncated:
        lines.append("Results truncated. Narrow the path or add a glob filter.")
    return "\n".join(lines)


def _path_variants(path: str) -> set[str]:
    return {path, path.replace("\\", "/"), path.replace("/", "\\")}


def _path_separator_for_style(path: str) -> str:
    return "\\" if "\\" in path and "/" not in path else "/"


def _join_path_preserving_style(base: str, relative: str) -> str:
    if not relative:
        return base
    separator = _path_separator_for_style(base)
    normalized_relative = relative.replace("\\" if separator == "/" else "/", separator).lstrip("/\\")
    stripped_base = base.rstrip("/\\")
    return f"{stripped_base}{separator}{normalized_relative}"


def _sanitize_error(error: Exception, runtime: Runtime | None = None) -> str:
    """Sanitize an error message to avoid leaking host filesystem paths.

    In local-sandbox mode, resolved host paths in the error string are masked
    back to their virtual equivalents so that user-visible output never exposes
    the host directory layout.
    """
    msg = f"{type(error).__name__}: {error}"
    if runtime is not None and is_local_sandbox(runtime):
        thread_data = get_thread_data(runtime)
        msg = mask_local_paths_in_output(msg, thread_data)
    return msg


def _truncate_write_file_error_detail(detail: str, max_chars: int) -> str:
    """Middle-truncate write_file error details, preserving the head and tail."""
    if max_chars == 0:
        return detail
    if len(detail) <= max_chars:
        return detail
    total = len(detail)
    marker_max_len = len(f"\n... [write_file error truncated: {total} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return detail[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total - kept
    marker = f"\n... [write_file error truncated: {skipped} chars skipped] ...\n"
    return f"{detail[:head_len]}{marker}{detail[-tail_len:] if tail_len > 0 else ''}"


def _format_write_file_error(
    requested_path: str,
    error: Exception,
    runtime: Runtime | None = None,
    *,
    max_chars: int = _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
) -> str:
    """Return a bounded, sanitized error string for write_file failures."""
    header = f"Error: Failed to write file '{requested_path}'"
    detail = _sanitize_error(error, runtime)
    if max_chars == 0:
        return f"{header}: {detail}"
    detail_budget = max_chars - len(header) - 2
    if detail_budget <= 0:
        return _truncate_write_file_error_detail(f"{header}: {detail}", max_chars)
    return f"{header}: {_truncate_write_file_error_detail(detail, detail_budget)}"


def replace_virtual_path(path: str, thread_data: ThreadDataState | None) -> str:
    """Replace virtual /mnt/user-data paths with actual thread data paths.

    Mapping:
        /mnt/user-data/workspace/* -> thread_data['workspace_path']/*
        /mnt/user-data/uploads/* -> thread_data['uploads_path']/*
        /mnt/user-data/outputs/* -> thread_data['outputs_path']/*

    Args:
        path: The path that may contain virtual path prefix.
        thread_data: The thread data containing actual paths.

    Returns:
        The path with virtual prefix replaced by actual path.
    """
    if thread_data is None:
        return path

    mappings = _thread_virtual_to_actual_mappings(thread_data)
    if not mappings:
        return path

    # Longest-prefix-first replacement with segment-boundary checks.
    for virtual_base, actual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        if path == virtual_base:
            return actual_base
        if path.startswith(f"{virtual_base}/"):
            rest = path[len(virtual_base) :].lstrip("/")
            result = _join_path_preserving_style(actual_base, rest)
            if path.endswith("/") and not result.endswith(("/", "\\")):
                result += _path_separator_for_style(actual_base)
            return result

    return path


def _thread_virtual_to_actual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    """Build virtual-to-actual path mappings for a thread."""
    mappings: dict[str, str] = {}

    workspace = thread_data.get("workspace_path")
    uploads = thread_data.get("uploads_path")
    outputs = thread_data.get("outputs_path")

    if workspace:
        mappings[f"{VIRTUAL_PATH_PREFIX}/workspace"] = workspace
    if uploads:
        mappings[f"{VIRTUAL_PATH_PREFIX}/uploads"] = uploads
    if outputs:
        mappings[f"{VIRTUAL_PATH_PREFIX}/outputs"] = outputs

    # Also map the virtual root when all known dirs share the same parent.
    actual_dirs = [Path(p) for p in (workspace, uploads, outputs) if p]
    if actual_dirs:
        common_parent = str(Path(actual_dirs[0]).parent)
        if all(str(path.parent) == common_parent for path in actual_dirs):
            mappings[VIRTUAL_PATH_PREFIX] = common_parent

    return mappings


def _thread_actual_to_virtual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    """Build actual-to-virtual mappings for output masking."""
    return {actual: virtual for virtual, actual in _thread_virtual_to_actual_mappings(thread_data).items()}


@lru_cache(maxsize=512)
def _compiled_mask_patterns(sources: tuple[tuple[str, str], ...]) -> tuple[tuple[re.Pattern[str], str, str], ...]:
    """Compile the host→virtual masking patterns once per source set.

    ``sources`` is an ordered tuple of ``(host_base, virtual_base)`` pairs
    (skills, then ACP workspace, then per-thread user-data mappings sorted by
    host-path length, longest first). The patterns derive only from
    config-stable + per-thread inputs, so they're cached and reused instead of
    being rebuilt — ``re.escape`` + ``re.compile`` + ``Path.resolve`` (a
    syscall) — on every call. ``mask_local_paths_in_output`` runs once per
    glob/grep match, so without this the same patterns are recompiled per
    match.
    """
    compiled: list[tuple[re.Pattern[str], str, str]] = []
    for host_base, virtual_base in sources:
        seen: set[str] = set()
        # Same base set as ``_path_variants(raw) | _path_variants(resolved)``;
        # ordered deterministically so the cached tuple is stable (variants of
        # one host map to the same virtual and don't overlap after substitution,
        # so order within a source is irrelevant to the result).
        for root in (str(Path(host_base)), str(Path(host_base).resolve())):
            for variant in sorted(_path_variants(root)):
                if variant in seen:
                    continue
                seen.add(variant)
                escaped = re.escape(variant).replace(r"\\", r"[/\\]")
                compiled.append((re.compile(escaped + r"(?:[/\\][^\s\"';&|<>()]*)?"), variant, virtual_base))
    return tuple(compiled)


def mask_local_paths_in_output(output: str, thread_data: ThreadDataState | None) -> str:
    """Mask host absolute paths from local sandbox output using virtual paths.

    Handles user-data paths (per-thread), skills paths, and ACP workspace paths (global).
    """
    # Build the ordered (host_base, virtual_base) source list. Order is
    # preserved from the original implementation: skills, then ACP workspace,
    # then user-data mappings (longest host path first). Custom mount host
    # paths are masked by LocalSandbox._reverse_resolve_paths_in_output().
    sources: list[tuple[str, str]] = []

    skills_host = _get_skills_host_path()
    if skills_host:
        sources.append((skills_host, _get_skills_container_path()))

    acp_host = _get_acp_workspace_host_path(_extract_thread_id_from_thread_data(thread_data))
    if acp_host:
        sources.append((acp_host, _ACP_WORKSPACE_VIRTUAL_PATH))

    if thread_data is not None:
        mappings = _thread_actual_to_virtual_mappings(thread_data)
        for actual_base, virtual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
            sources.append((actual_base, virtual_base))

    if not sources:
        return output

    result = output
    for pattern, base, virtual in _compiled_mask_patterns(tuple(sources)):

        def replace_match(match: re.Match, _base: str = base, _virtual: str = virtual) -> str:
            matched_path = match.group(0)
            if matched_path == _base:
                return _virtual
            relative = matched_path[len(_base) :].lstrip("/\\")
            return f"{_virtual}/{relative}" if relative else _virtual

        result = pattern.sub(replace_match, result)

    return result


def _reject_path_traversal(path: str) -> None:
    """Reject paths that contain '..' segments to prevent directory traversal."""
    # Normalise to forward slashes, then check for '..' segments.
    normalised = path.replace("\\", "/")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PermissionError("Access denied: path traversal detected")


def validate_local_tool_path(path: str, thread_data: ThreadDataState | None, *, read_only: bool = False) -> None:
    """Validate that a virtual path is allowed for local-sandbox access.

    This function is a security gate — it checks whether *path* may be
    accessed and raises on violation.  It does **not** resolve the virtual
    path to a host path; callers are responsible for resolution via
    ``resolve_and_validate_user_data_path`` or ``_resolve_skills_path``.

    Allowed virtual-path families:
      - ``/mnt/user-data/*``  — always allowed (read + write)
      - ``/mnt/skills/*``     — allowed only when *read_only* is True
      - ``/mnt/acp-workspace/*`` — allowed only when *read_only* is True
      - Custom mount paths (from config.yaml) — respects per-mount ``read_only`` flag

    Args:
        path: The virtual path to validate.
        thread_data: Thread data (must be present for local sandbox).
        read_only: When True, skills and ACP workspace paths are permitted.

    Raises:
        SandboxRuntimeError: If thread data is missing.
        PermissionError: If the path is not allowed or contains traversal.
    """
    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for local sandbox")

    _reject_path_traversal(path)

    # Skills paths — read-only through the file tools. Custom skills ARE writable, but only
    # via ``skill_manage`` (security scan + edit history), so say so instead of just refusing.
    if _is_skills_path(path):
        if not read_only:
            raise PermissionError(f"Write access to skills path is not allowed: {path}{_skills_write_hint(path)}")
        return

    # ACP workspace paths — read-only access only
    if _is_acp_workspace_path(path):
        if not read_only:
            raise PermissionError(f"Write access to ACP workspace is not allowed: {path}")
        return

    # The bare virtual root is a UNION of three host directories in a local sandbox, so it
    # cannot be resolved to one path. Checked BEFORE the prefix test so the trailing-slash
    # form lands here too: it used to pass validation and then fail deep in path resolution
    # as "path traversal detected", which is both alarming and wrong. An agent that cannot
    # tell which part of its call is wrong retries it with cosmetic edits, so name the three
    # concrete roots.
    if path.rstrip("/") == VIRTUAL_PATH_PREFIX:
        raise PermissionError(
            f"{VIRTUAL_PATH_PREFIX} is not a single directory here — it is the union of "
            f"{VIRTUAL_PATH_PREFIX}/workspace, {VIRTUAL_PATH_PREFIX}/uploads and {VIRTUAL_PATH_PREFIX}/outputs. "
            "Pick one of those three as the root, or run the call once per root."
        )

    # User-data paths
    if path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        return

    # Custom mount paths — respect read_only config
    if _is_custom_mount_path(path):
        mount = _get_custom_mount_for_path(path)
        if mount and mount.read_only and not read_only:
            raise PermissionError(f"Write access to read-only mount is not allowed: {path}")
        return

    raise PermissionError(f"Only paths under {VIRTUAL_PATH_PREFIX}/, {_get_skills_container_path()}/, {_ACP_WORKSPACE_VIRTUAL_PATH}/, or configured mount paths are allowed")


def _validate_resolved_user_data_path(resolved: Path, thread_data: ThreadDataState) -> None:
    """Verify that a resolved host path stays inside allowed per-thread roots.

    Raises PermissionError if the path escapes workspace/uploads/outputs.
    """
    allowed_roots = [
        Path(p).resolve()
        for p in (
            thread_data.get("workspace_path"),
            thread_data.get("uploads_path"),
            thread_data.get("outputs_path"),
        )
        if p is not None
    ]

    if not allowed_roots:
        raise SandboxRuntimeError("No allowed local sandbox directories configured")

    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue

    raise PermissionError("Access denied: path traversal detected")


def _resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """Resolve a /mnt/user-data virtual path and validate it stays in bounds.

    Returns the resolved host path string.
    """
    resolved_str = replace_virtual_path(path, thread_data)
    resolved = Path(resolved_str).resolve()
    _validate_resolved_user_data_path(resolved, thread_data)
    return str(resolved)


def _is_non_file_url_token(token: str) -> bool:
    """Return True for URL tokens that should not be interpreted as paths."""
    values = [token]
    if "=" in token:
        values.append(token.split("=", 1)[1])

    for value in values:
        match = _URL_WITH_SCHEME_PATTERN.match(value)
        if match and not value.lower().startswith("file://"):
            return True
    return False


def _non_file_url_spans(command: str) -> list[tuple[int, int]]:
    spans = []
    for match in _URL_IN_COMMAND_PATTERN.finditer(command):
        if not match.group().lower().startswith("file://"):
            spans.append(match.span())
    return spans


def _is_in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


#: ``<<'EOF'`` / ``<<"EOF"`` (optionally ``<<-``). Only the *quoted* forms are matched:
#: an unquoted ``<<EOF`` still expands ``$VAR`` and command substitutions, so its body can
#: become a real host path at runtime and must stay under the guard.
_QUOTED_HEREDOC_START_PATTERN = re.compile(r"<<-?\s*(?P<quote>['\"])(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")


def _quoted_heredoc_body_spans(command: str) -> list[tuple[int, int]]:
    """Spans of quoted-heredoc bodies, which are literal text and not shell paths.

    The absolute-path scan runs over the raw command string, so a ``/segment`` sequence
    anywhere in an embedded script reads as a path candidate — including inside the Python
    source of ``python3 << 'PYEOF' ... PYEOF``. Session ``a7c19ea1`` lost four turns to
    this while already stuck in an unrelated loop: a ``re.sub`` pattern, a ``grep`` needle
    and two dict keys were reported as ``Unsafe absolute paths in command: /Users/``,
    ``/pid/``, ``/source`` — none of which the command would ever open.

    A quoted delimiter is the precise signal that makes this safe to exempt: bash performs
    no expansion inside it, so the body is exactly the bytes written and cannot resolve to
    a host path the caller did not spell out. The unquoted form is deliberately left
    enforced.

    An unterminated heredoc extends to end of command — that is also how the shell reads
    it, so nothing outside the body silently loses its check.
    """
    spans: list[tuple[int, int]] = []
    for match in _QUOTED_HEREDOC_START_PATTERN.finditer(command):
        body_start = command.find("\n", match.end())
        if body_start == -1:
            continue
        body_start += 1
        tag = match.group("tag")
        end = len(command)
        for line_match in re.finditer(rf"^[ \t]*{re.escape(tag)}[ \t]*$", command[body_start:], re.MULTILINE):
            end = body_start + line_match.start()
            break
        spans.append((body_start, end))
    return spans


def _has_dotdot_path_segment(token: str) -> bool:
    if _is_non_file_url_token(token):
        return False
    return bool(_DOTDOT_PATH_SEGMENT_PATTERN.search(token))


def _split_shell_tokens(command: str) -> list[str]:
    try:
        normalized = command.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ; ")
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        # The shell will reject malformed quoting later; keep validation
        # best-effort instead of turning syntax errors into security messages.
        return command.split()


def _is_shell_command_separator(token: str) -> bool:
    return token in _SHELL_COMMAND_SEPARATORS


def _is_shell_redirection_operator(token: str) -> bool:
    return token in _SHELL_REDIRECTION_OPERATORS


def _is_shell_assignment(token: str) -> bool:
    name, separator, _ = token.partition("=")
    if not separator or not name:
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _is_allowed_local_bash_absolute_path(path: str, allowed_paths: list[str], *, allow_system_paths: bool) -> bool:
    # Check for MCP filesystem server allowed paths
    if any(path.startswith(allowed_path) or path == allowed_path.rstrip("/") for allowed_path in allowed_paths):
        _reject_path_traversal(path)
        return True

    if path == VIRTUAL_PATH_PREFIX or path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        _reject_path_traversal(path)
        return True

    # Allow skills container path (resolved by tools.py before passing to sandbox)
    if _is_skills_path(path):
        _reject_path_traversal(path)
        return True

    # Allow ACP workspace path (path-traversal check only)
    if _is_acp_workspace_path(path):
        _reject_path_traversal(path)
        return True

    # Allow custom mount container paths
    if _is_custom_mount_path(path):
        _reject_path_traversal(path)
        return True

    if allow_system_paths and any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _LOCAL_BASH_SYSTEM_PATH_PREFIXES):
        return True

    return False


def _next_cd_target(tokens: list[str], start_index: int) -> tuple[str | None, int]:
    index = start_index
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_command_separator(token):
            return None, index
        if _is_shell_redirection_operator(token):
            index += 2
            continue
        if token == "--":
            index += 1
            continue
        if token in {"-L", "-P", "-e", "-@"}:
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return token, index + 1
    return None, index


def _validate_local_bash_cwd_target(command_name: str, target: str | None, allowed_paths: list[str]) -> None:
    if target is None or target == "-":
        raise PermissionError(f"Unsafe working directory change in command: {command_name}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith(("$", "`")):
        raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith("~"):
        raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith("/"):
        _reject_path_traversal(target)
        if not _is_allowed_local_bash_absolute_path(target, allowed_paths, allow_system_paths=False):
            raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")


def _validate_local_bash_root_path_args(command_name: str, tokens: list[str], start_index: int) -> None:
    if command_name not in _LOCAL_BASH_ROOT_PATH_COMMANDS:
        return

    index = start_index
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_command_separator(token):
            return
        if _is_shell_redirection_operator(token):
            index += 2
            continue
        if token == "/" and not _is_non_file_url_token(token):
            raise PermissionError(f"Unsafe absolute paths in command: /. Use paths under {VIRTUAL_PATH_PREFIX}")
        index += 1


def _validate_local_bash_shell_tokens(command: str, allowed_paths: list[str]) -> None:
    """Conservatively reject relative path escapes missed by absolute-path scanning."""
    if re.search(r"\$\([^)]*\b(?:cd|pushd)\b", command):
        raise PermissionError(f"Unsafe working directory change in command substitution. Use paths under {VIRTUAL_PATH_PREFIX}")

    tokens = _split_shell_tokens(command)

    for token in tokens:
        if _is_shell_command_separator(token) or _is_shell_redirection_operator(token):
            continue
        if _has_dotdot_path_segment(token):
            raise PermissionError("Access denied: path traversal detected")

    at_command_start = True
    index = 0
    while index < len(tokens):
        token = tokens[index]

        if _is_shell_command_separator(token):
            at_command_start = True
            index += 1
            continue

        if _is_shell_redirection_operator(token):
            index += 1
            continue

        if at_command_start and _is_shell_assignment(token):
            index += 1
            continue

        command_name = token.rsplit("/", 1)[-1]
        if at_command_start and command_name in _LOCAL_BASH_COMMAND_PREFIX_KEYWORDS | _LOCAL_BASH_COMMAND_END_KEYWORDS:
            index += 1
            continue

        if not at_command_start:
            index += 1
            continue

        at_command_start = False
        if command_name in _LOCAL_BASH_COMMAND_WRAPPERS and index + 1 < len(tokens):
            wrapped_name = tokens[index + 1].rsplit("/", 1)[-1]
            if wrapped_name in _LOCAL_BASH_CWD_COMMANDS:
                target, next_index = _next_cd_target(tokens, index + 2)
                _validate_local_bash_cwd_target(wrapped_name, target, allowed_paths)
                index = next_index
                continue
            _validate_local_bash_root_path_args(wrapped_name, tokens, index + 2)

        if command_name not in _LOCAL_BASH_CWD_COMMANDS:
            _validate_local_bash_root_path_args(command_name, tokens, index + 1)
            index += 1
            continue

        target, next_index = _next_cd_target(tokens, index + 1)
        _validate_local_bash_cwd_target(command_name, target, allowed_paths)
        index = next_index


def resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """Resolve a /mnt/user-data virtual path and validate it stays in bounds."""
    return _resolve_and_validate_user_data_path(path, thread_data)


def _braces_are_identifier_placeholders_only(fragment: str) -> bool:
    """Return True only if every ``{...}`` block is a single identifier placeholder.

    Identifier-only blocks (``{id}``, ``{port}``) come from REST templates and
    f-strings and are text. Bash brace expansion (``{passwd,shadow}``, ``{,.bak}``,
    ``{etc,var}``) reconstitutes real host paths at runtime, so it must NOT be
    exempted. Stray, empty, or nested braces are rejected too (each ``{``/``}``
    must belong to one balanced single-placeholder block).

    ``${VAR}`` shell variable expansion (e.g. ``/home/${USER}/.ssh/id_rsa``) also
    expands to a real host path at runtime, so a ``${`` anywhere disqualifies the
    fragment even though the inner name is identifier-shaped.
    """
    if "${" in fragment:
        return False
    blocks = _IDENTIFIER_BRACE_BLOCK_PATTERN.findall(fragment)
    # Every brace must be part of a balanced ``{...}`` block (no stray/nested braces).
    if fragment.count("{") != len(blocks) or fragment.count("}") != len(blocks):
        return False
    return all(_IDENTIFIER_PATTERN.fullmatch(inner) for inner in blocks)


def _is_non_path_literal_fragment(fragment: str) -> bool:
    """Return True if a ``/segment`` match is almost certainly text, not a path.

    The absolute-path scan runs over the raw command string, so it also matches
    ``/segment`` sequences sitting inside string literals, f-strings, and
    templates (e.g. ``python -c "print(f'/端口{port}')"`` or a REST template
    like ``/devices/{id}/port``). Non-ASCII characters and single identifier-like
    ``{placeholder}`` braces do not appear in real host filesystem paths a command
    would open, so treating such fragments as text removes those false positives.

    Bash brace expansion (``cat /etc/{passwd,shadow}``) is deliberately NOT
    exempted: it expands to plain host paths at runtime, so only braces that are
    single identifier placeholders are treated as text (see
    :func:`_braces_are_identifier_placeholders_only`).

    This guard is best-effort, not a security boundary (see
    :func:`validate_local_bash_command_paths`): plain ASCII host paths such as
    ``/etc/passwd`` contain none of these markers and are still rejected.
    """
    if any(ord(ch) > 127 for ch in fragment):
        return True
    if "{" in fragment or "}" in fragment:
        return _braces_are_identifier_placeholders_only(fragment)
    return False


def _is_shell_regex_literal_fragment(fragment: str, standalone_tokens: frozenset[str]) -> bool:
    """Return True if a ``/segment`` match is a ``sed``/``awk`` regex body, not a path.

    The absolute-path scan runs over the raw command string, so slash-delimited regex
    bodies surface as path candidates. Session ``c2518bc7`` lost three consecutive AI
    steps to exactly this: ``awk -F: 'NR>=5 && /^[0-9]+:/'`` and ``sed 's/．//'`` were
    both rejected as "Unsafe absolute paths", an error the model cannot act on because
    the command contains no host path at all.

    Three shapes, each chosen because it cannot name a file a command would open:

    * The segment starts with ``^`` — a regex anchor. No host path begins with ``/^``.
    * Braces are unbalanced (``/{print``). Bash brace expansion requires a matching
      ``}``, so an unbalanced brace is neither a path nor an expansion. Balanced
      non-identifier braces (``/{etc,var}/passwd``) are deliberately left to
      :func:`_braces_are_identifier_placeholders_only`, which keeps them blocked.
    * The fragment is nothing but slashes — the tail of a ``s/x//`` replacement. This
      one is exempt **only when it is not a standalone shell token**, so genuine root
      targeting (``rm -rf //``) stays blocked while an embedded replacement body does
      not trip the guard.

    Best-effort, like the rest of :func:`validate_local_bash_command_paths`: none of
    these shapes widens reach to a real host path.
    """
    body = fragment.lstrip("/")
    if not body:
        return fragment not in standalone_tokens
    if body.startswith("^"):
        return True
    return ("{" in fragment or "}" in fragment) and fragment.count("{") != fragment.count("}")


def validate_local_bash_command_paths(command: str, thread_data: ThreadDataState | None) -> None:
    """Validate absolute paths in local-sandbox bash commands.

    This validation is only a best-effort guard for the explicit
    ``sandbox.allow_host_bash: true`` opt-in. It is not a secure sandbox
    boundary and must not be treated as isolation from the host filesystem.

    In local mode, commands must use virtual paths under /mnt/user-data for
    user data access. Skills paths under /mnt/skills, ACP workspace paths
    under /mnt/acp-workspace, and custom mount container paths (configured in
    config.yaml) are allowed (path-traversal checks only; write prevention
    for bash commands is not enforced here).
    A small allowlist of common system path prefixes is kept for executable
    and device references (e.g. /bin/sh, /dev/null).
    """
    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for local sandbox")

    # Block file:// URLs which bypass the absolute-path regex but allow local file exfiltration
    file_url_match = _FILE_URL_PATTERN.search(command)
    if file_url_match:
        raise PermissionError(f"Unsafe file:// URL in command: {file_url_match.group()}. Use paths under {VIRTUAL_PATH_PREFIX}")

    unsafe_paths: list[str] = []
    allowed_paths = _get_mcp_allowed_paths()
    _validate_local_bash_shell_tokens(command, allowed_paths)
    url_spans = _non_file_url_spans(command)
    # Literal text the shell will not expand: quoted-heredoc bodies. Treated exactly like
    # URL spans — the scan skips matches inside them (see _quoted_heredoc_body_spans).
    literal_spans = url_spans + _quoted_heredoc_body_spans(command)
    standalone_tokens = frozenset(_split_shell_tokens(command))

    for match in _ABSOLUTE_PATH_PATTERN.finditer(command):
        if _is_in_spans(match.start(), literal_spans):
            continue
        absolute_path = match.group()
        if _is_non_path_literal_fragment(absolute_path):
            continue
        if _is_shell_regex_literal_fragment(absolute_path, standalone_tokens):
            continue
        if _is_allowed_local_bash_absolute_path(absolute_path, allowed_paths, allow_system_paths=True):
            continue

        unsafe_paths.append(absolute_path)

    if unsafe_paths:
        unsafe = ", ".join(sorted(dict.fromkeys(unsafe_paths)))
        raise PermissionError(f"Unsafe absolute paths in command: {unsafe}. Use paths under {VIRTUAL_PATH_PREFIX}{_unexpanded_variable_hint(command, unsafe_paths)}")


def _unexpanded_variable_hint(command: str, unsafe_paths: list[str]) -> str:
    """Extra hint when the offending path looks like a variable that never expanded.

    ``cd "$WORKSPACE/out"`` with ``WORKSPACE`` unset becomes ``cd "/out"`` — the command is
    rejected for a path the author never wrote, so the message ("use paths under
    /mnt/user-data") describes a mistake they cannot find in their own command. Observed
    result: the same command re-sent with cosmetic quoting changes.

    Only fires when the command actually references a shell variable, so a plainly wrong
    absolute path still gets the plain message.
    """
    if not _SHELL_VARIABLE_PATTERN.search(command):
        return ""
    suspicious = [p for p in unsafe_paths if p.count("/") <= 2]
    if not suspicious:
        return ""
    return f". This command references a shell variable — if it is unset, `$VAR/sub` expands to just `/sub`, which is what was rejected here. Echo the variable first, or write the path literally under {VIRTUAL_PATH_PREFIX}"


def replace_virtual_paths_in_command(command: str, thread_data: ThreadDataState | None) -> str:
    """Replace all virtual paths (/mnt/user-data, /mnt/skills, /mnt/acp-workspace) in a command string.

    Args:
        command: The command string that may contain virtual paths.
        thread_data: The thread data containing actual paths.

    Returns:
        The command with all virtual paths replaced.
    """
    result = command

    # Replace skills paths
    skills_container = _get_skills_container_path()
    skills_host = _get_skills_host_path()
    if skills_host and skills_container in result:
        skills_pattern = re.compile(rf"{re.escape(skills_container)}(/[^\s\"';&|<>()]*)?")

        def replace_skills_match(match: re.Match) -> str:
            return _resolve_skills_path(match.group(0)).replace("\\", "/")

        result = skills_pattern.sub(replace_skills_match, result)

    # Replace ACP workspace paths
    _thread_id = _extract_thread_id_from_thread_data(thread_data)
    acp_host = _get_acp_workspace_host_path(_thread_id)
    if acp_host and _ACP_WORKSPACE_VIRTUAL_PATH in result:
        acp_pattern = re.compile(rf"{re.escape(_ACP_WORKSPACE_VIRTUAL_PATH)}(/[^\s\"';&|<>()]*)?")

        def replace_acp_match(match: re.Match, _tid: str | None = _thread_id) -> str:
            return _resolve_acp_workspace_path(match.group(0), _tid).replace("\\", "/")

        result = acp_pattern.sub(replace_acp_match, result)

    # Custom mount paths are resolved by LocalSandbox._resolve_paths_in_command()

    # Replace user-data paths
    if VIRTUAL_PATH_PREFIX in result and thread_data is not None:
        pattern = re.compile(rf"{re.escape(VIRTUAL_PATH_PREFIX)}(/[^\s\"';&|<>()]*)?")

        def replace_user_data_match(match: re.Match) -> str:
            return replace_virtual_path(match.group(0), thread_data).replace("\\", "/")

        result = pattern.sub(replace_user_data_match, result)

    return result


def _apply_cwd_prefix(command: str, thread_data: ThreadDataState | None) -> str:
    """Prepend 'cd <workspace> &&' so relative paths are anchored to the thread workspace.

    Args:
        command: The bash command to execute.
        thread_data: The thread data containing the workspace path.

    Returns:
        The command prefixed with 'cd <workspace> &&' if workspace_path is available,
        otherwise the original command unchanged.
    """
    if thread_data and (workspace := thread_data.get("workspace_path")):
        return f"cd {shlex.quote(workspace)} && {command}"
    return command


def get_thread_data(runtime: Runtime | None) -> ThreadDataState | None:
    """Extract thread_data from runtime state."""
    if runtime is None:
        return None
    if runtime.state is None:
        return None
    return runtime.state.get("thread_data")


def is_local_sandbox(runtime: Runtime | None) -> bool:
    """Check if the current sandbox is a local sandbox.

    Accepts both the generic id ``"local"`` (acquire with no thread context)
    and the per-thread id format ``"local:{user_id}:{thread_id}"`` produced
    by :meth:`LocalSandboxProvider.acquire` once a thread is known.
    """
    if runtime is None:
        return False
    if runtime.state is None:
        return False
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is None:
        return False
    sandbox_id = sandbox_state.get("sandbox_id")
    if not isinstance(sandbox_id, str):
        return False
    return sandbox_id == "local" or sandbox_id.startswith("local:")


def sandbox_from_runtime(runtime: Runtime | None = None) -> Sandbox:
    """Extract sandbox instance from tool runtime.

    DEPRECATED: Use ensure_sandbox_initialized() for lazy initialization support.
    This function assumes sandbox is already initialized and will raise error if not.

    Raises:
        SandboxRuntimeError: If runtime is not available or sandbox state is missing.
        SandboxNotFoundError: If sandbox with the given ID cannot be found.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")
    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is None:
        raise SandboxRuntimeError("Sandbox state not initialized in runtime")
    sandbox_id = sandbox_state.get("sandbox_id")
    if sandbox_id is None:
        raise SandboxRuntimeError("Sandbox ID not found in state")
    sandbox = get_sandbox_provider().get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError(f"Sandbox with ID '{sandbox_id}' not found", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for downstream use
    return sandbox


def ensure_sandbox_initialized(runtime: Runtime | None = None) -> Sandbox:
    """Ensure sandbox is initialized, acquiring lazily if needed.

    On first call, acquires a sandbox from the provider and stores it in runtime state.
    Subsequent calls return the existing sandbox.

    Thread-safety is guaranteed by the provider's internal locking mechanism.

    Args:
        runtime: Tool runtime containing state and context.

    Returns:
        Initialized sandbox instance.

    Raises:
        SandboxRuntimeError: If runtime is not available or thread_id is missing.
        SandboxNotFoundError: If sandbox acquisition fails.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")

    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")

    # Check if sandbox already exists in state
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for releasing in after_agent
                return sandbox
            # Sandbox was released, fall through to acquire new one

    # Lazy acquisition: get thread_id and acquire sandbox
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = provider.acquire(thread_id, user_id=resolve_runtime_user_id(runtime))

    # Update runtime state - this persists across tool calls
    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    # Retrieve and return the sandbox
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for releasing in after_agent
    return sandbox


async def ensure_sandbox_initialized_async(runtime: Runtime | None = None) -> Sandbox:
    """Async counterpart to ``ensure_sandbox_initialized`` for tool runtimes.

    This keeps lazy sandbox acquisition on the async provider hook, so AIO
    sandbox startup and readiness polling do not fall back to synchronous
    ``provider.acquire()`` during async tool execution.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")

    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")

    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id
                return sandbox

    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = await provider.acquire_async(thread_id, user_id=resolve_runtime_user_id(runtime))

    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id
    return sandbox


async def _run_sync_tool_after_async_sandbox_init(
    func: Callable[..., str] | None,
    runtime: Runtime,
    *args: object,
) -> str:
    """Initialize lazily via async provider, then run sync tool body off-thread."""
    try:
        await ensure_sandbox_initialized_async(runtime)
    except SandboxError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: Unexpected error initializing sandbox: {_sanitize_error(e, runtime)}"

    if func is None:
        return "Error: Tool implementation not available"

    return await asyncio.to_thread(func, runtime, *args)


def ensure_thread_directories_exist(runtime: Runtime | None) -> None:
    """Ensure thread data directories (workspace, uploads, outputs) exist.

    This function is called lazily when any sandbox tool is first used.
    For local sandbox, it creates the directories on the filesystem.
    For other sandboxes (like aio), directories are already mounted in the container.

    Args:
        runtime: Tool runtime containing state and context.
    """
    if runtime is None:
        return

    # Only create directories for local sandbox
    if not is_local_sandbox(runtime):
        return

    thread_data = get_thread_data(runtime)
    if thread_data is None:
        return

    # Check if directories have already been created
    if runtime.state.get("thread_directories_created"):
        return

    # Create the three directories
    import os

    for key in ["workspace_path", "uploads_path", "outputs_path"]:
        path = thread_data.get(key)
        if path:
            os.makedirs(path, exist_ok=True)

    # Mark as created to avoid redundant operations
    runtime.state["thread_directories_created"] = True


_SECRET_REDACTION = "[redacted]"

# Values shorter than this are not redacted from bash output. A short secret
# value (a 2-char region code, a numeric id, a PIN) would otherwise shred
# unrelated bytes of tool output — exit codes, timestamps, sizes, paths —
# corrupting the result the model reads back. The redaction of a value this
# short is more likely noise than genuine leak protection; the secret is still
# injected into the subprocess, only the output mask skips it.
_MIN_MASK_LENGTH = 8


def mask_secret_values(output: str, injected_env: dict[str, str] | None) -> str:
    """Redact injected secret values from bash output before it re-enters context.

    Skill scripts receive request-scoped secrets as env vars (#3861). If a script
    echoes one (debugging, ``set -x``, an error dump), the value would otherwise
    flow into the tool result — and thus into the prompt and the trace. This is
    the skill-specific fifth leak surface (the bash tool returns subprocess stdout,
    unlike MCP tools). Replace each non-empty secret value with a redaction marker.
    Longest values first so a value that is a substring of another is not partially
    revealed. Values shorter than ``_MIN_MASK_LENGTH`` are skipped — a redacted
    3-char token is more likely to corrupt unrelated output than to protect a
    real secret.
    """
    if not injected_env or not output:
        return output
    for value in sorted((v for v in injected_env.values() if v and len(v) >= _MIN_MASK_LENGTH), key=len, reverse=True):
        output = output.replace(value, _SECRET_REDACTION)
    return output


def _truncate_bash_output(output: str, max_chars: int) -> str:
    """Middle-truncate bash output, preserving head and tail (50/50 split).

    bash output may have errors at either end (stderr/stdout ordering is
    non-deterministic), so both ends are preserved equally.

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total_len = len(output)
    # Compute the exact worst-case marker length: skipped chars is at most
    # total_len, so this is a tight upper bound.
    marker_max_len = len(f"\n... [middle truncated: {total_len} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total_len - kept
    marker = f"\n... [middle truncated: {skipped} chars skipped] ...\n"
    return f"{output[:head_len]}{marker}{output[-tail_len:] if tail_len > 0 else ''}"


def _truncate_read_file_output(output: str, max_chars: int, *, total_lines: int | None = None) -> str:
    """Head-truncate read_file output, preserving the beginning of the file.

    Source code and documents are read top-to-bottom; the head contains the
    most context (imports, class definitions, function signatures).

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.

    ``total_lines`` makes the hint actionable. The previous marker said only
    "Use start_line/end_line to read a specific range", and session ``c2518bc7``
    shows what the model does with that: it asks for ``start_line=60`` with no
    ``end_line``, gets the head back (see ``read_file_tool``), decides the file is
    "repeating", and spends an extra step on ``bash wc -l`` just to learn the file
    length. Naming the length and a *closed* range removes both round-trips.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total = len(output)
    hint = f"Use start_line/end_line to read a specific range (file has {total_lines} lines; e.g. start_line=1, end_line={total_lines})" if total_lines else "Use start_line/end_line to read a specific range"
    # Compute the exact worst-case marker length: both numeric fields are at
    # their maximum (total chars), so this is a tight upper bound.
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. {hint}] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. {hint}] ..."
    return f"{output[:kept]}{marker}"


def _truncate_ls_output(output: str, max_chars: int) -> str:
    """Head-truncate ls output, preserving the beginning of the listing.

    Directory listings are read top-to-bottom; the head shows the most
    relevant structure.

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total = len(output)
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. Use a more specific path to see fewer results] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. Use a more specific path to see fewer results] ..."
    return f"{output[:kept]}{marker}"


@tool("bash", parse_docstring=True)
def bash_tool(runtime: Runtime, description: str, command: str) -> str:
    """Execute a bash command in a Linux environment.

    IMPORTANT — bash operates in a LOCAL SANDBOX with NO internet access. It CANNOT:
    - Search the web → use `web_search` instead
    - Fetch web pages → use `web_fetch` instead
    - Access any network resources or external APIs

    bash is designed for LOCAL operations only:
    - Running Python scripts and installing packages
    - File system operations (move, copy, delete, permission changes)
    - Building, compiling, testing code
    - Data processing and analysis with local tools

    ANTI-PATTERNS — DO NOT use bash for:
    - Searching the web → use `web_search` (NOT curl/wget/python requests)
    - Fetching web pages → use `web_fetch` (NOT curl/wget/python requests)
    - Reading files → use `read_file` (NOT cat)
    - Listing directories → use `ls` (NOT ls in bash)
    - Searching file contents → use `grep` (NOT grep in bash)
    - Finding files → use `glob` (NOT find in bash)
    - Editing files → use `str_replace` or `write_file` (NOT sed/awk/echo)
    If a dedicated tool exists for the task, use it. Writing Python scripts or
    shell pipelines to replicate a tool's functionality is wasteful and wrong.

    - Use `python` to run Python code.
    - Prefer a thread-local virtual environment in `/mnt/user-data/workspace/.venv`.
    - Use `python -m pip` (inside the virtual environment) to install Python packages.
    - To start a long-lived process such as a web server, ALWAYS run it in the background with its
      output redirected, e.g. `your-command > /mnt/user-data/workspace/server.log 2>&1 &`, then check
      the log file or poll the port. A long-lived process run in the foreground blocks the turn until
      it is killed at the command timeout.

    Args:
        description: Explain why you are running this command in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        command: The bash command to execute. Always use absolute paths for files and directories.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        # Request-scoped secrets resolved for the active skill (#3861); injected as
        # per-call env into the subprocess, never placed in the command string.
        injected_env = read_active_secrets(getattr(runtime, "context", None)) or None
        if is_local_sandbox(runtime):
            if not is_host_bash_allowed():
                return f"Error: {LOCAL_HOST_BASH_DISABLED_MESSAGE}"
            ensure_thread_directories_exist(runtime)
            thread_data = get_thread_data(runtime)
            validate_local_bash_command_paths(command, thread_data)
            command = replace_virtual_paths_in_command(command, thread_data)
            command = _apply_cwd_prefix(command, thread_data)
            try:
                from deerflow.config.app_config import get_app_config

                sandbox_cfg = get_app_config().sandbox
                max_chars = sandbox_cfg.bash_output_max_chars if sandbox_cfg else 20000
                command_timeout = sandbox_cfg.bash_command_timeout if sandbox_cfg else None
            except Exception:
                max_chars = 20000
                command_timeout = None
            output = sandbox.execute_command(command, env=injected_env, timeout=command_timeout)
            return _truncate_bash_output(
                mask_secret_values(mask_local_paths_in_output(output, thread_data), injected_env),
                max_chars,
            )
        ensure_thread_directories_exist(runtime)
        try:
            from deerflow.config.app_config import get_app_config

            sandbox_cfg = get_app_config().sandbox
            max_chars = sandbox_cfg.bash_output_max_chars if sandbox_cfg else 20000
        except Exception:
            max_chars = 20000
        return _truncate_bash_output(mask_secret_values(sandbox.execute_command(command, env=injected_env), injected_env), max_chars)
    except SandboxError as e:
        return f"Error: {e}"
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: Unexpected error executing command: {_sanitize_error(e, runtime)}"


async def _bash_tool_async(runtime: Runtime, description: str, command: str) -> str:
    return await _run_sync_tool_after_async_sandbox_init(bash_tool.func, runtime, description, command)


bash_tool.coroutine = _bash_tool_async


@tool("ls", parse_docstring=True)
def ls_tool(runtime: Runtime, description: str, path: str) -> str:
    """List the contents of a directory up to 2 levels deep in tree format.

    Args:
        description: Explain why you are listing this directory in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the directory to list.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data, read_only=True)
            if _is_skills_path(path):
                path = _resolve_skills_path(path)
            elif _is_acp_workspace_path(path):
                path = _resolve_acp_workspace_path(path, _extract_thread_id_from_thread_data(thread_data))
            elif not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()
        children = sandbox.list_dir(path)
        if not children:
            return "(empty)"
        output = "\n".join(children)
        if thread_data is not None:
            output = mask_local_paths_in_output(output, thread_data)
        try:
            from deerflow.config.app_config import get_app_config

            sandbox_cfg = get_app_config().sandbox
            max_chars = sandbox_cfg.ls_output_max_chars if sandbox_cfg else 20000
        except Exception:
            max_chars = 20000
        return _truncate_ls_output(output, max_chars)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except PermissionError as e:
        # Surface the reason: a bare "Permission denied" gives an agent nothing to act on,
        # and the interesting cases (virtual root is a union, read-only mount) each carry a
        # specific corrective message.
        return f"Error: Permission denied: {requested_path}{f' — {e}' if str(e) else ''}"
    except Exception as e:
        return f"Error: Unexpected error listing directory: {_sanitize_error(e, runtime)}"


async def _ls_tool_async(runtime: Runtime, description: str, path: str) -> str:
    return await _run_sync_tool_after_async_sandbox_init(ls_tool.func, runtime, description, path)


ls_tool.coroutine = _ls_tool_async


@tool("glob", parse_docstring=True)
def glob_tool(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    include_dirs: bool = False,
    max_results: int = _DEFAULT_GLOB_MAX_RESULTS,
) -> str:
    """Find files or directories that match a glob pattern under a root directory.

    Args:
        description: Explain why you are searching for these paths in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        pattern: The glob pattern to match relative to the root path, for example `**/*.py`.
        path: The **absolute** root directory to search under.
        include_dirs: Whether matching directories should also be returned. Default is False.
        max_results: Maximum number of paths to return. Default is 200.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        effective_max_results = _resolve_max_results(
            "glob",
            max_results,
            default=_DEFAULT_GLOB_MAX_RESULTS,
            upper_bound=_MAX_GLOB_MAX_RESULTS,
        )
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            if thread_data is None:
                raise SandboxRuntimeError("Thread data not available for local sandbox")
            path = _resolve_local_read_path(path, thread_data)
        matches, truncated = sandbox.glob(path, pattern, include_dirs=include_dirs, max_results=effective_max_results)
        if thread_data is not None:
            matches = [mask_local_paths_in_output(match, thread_data) for match in matches]
        return _format_glob_results(requested_path, matches, truncated)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except NotADirectoryError:
        return f"Error: Path is not a directory: {requested_path}"
    except PermissionError as e:
        # Surface the reason: a bare "Permission denied" gives an agent nothing to act on,
        # and the interesting cases (virtual root is a union, read-only mount) each carry a
        # specific corrective message.
        return f"Error: Permission denied: {requested_path}{f' — {e}' if str(e) else ''}"
    except Exception as e:
        return f"Error: Unexpected error searching paths: {_sanitize_error(e, runtime)}"


async def _glob_tool_async(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    include_dirs: bool = False,
    max_results: int = _DEFAULT_GLOB_MAX_RESULTS,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        glob_tool.func,
        runtime,
        description,
        pattern,
        path,
        include_dirs,
        max_results,
    )


glob_tool.coroutine = _glob_tool_async


@tool("grep", parse_docstring=True)
def grep_tool(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = _DEFAULT_GREP_MAX_RESULTS,
) -> str:
    """Search for matching lines inside text files under a root directory.

    Args:
        description: Explain why you are searching file contents in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        pattern: The string or regex pattern to search for.
        path: The **absolute** root directory to search under.
        glob: Optional glob filter for candidate files, for example `**/*.py`.
        literal: Whether to treat `pattern` as a plain string. Default is False.
        case_sensitive: Whether matching is case-sensitive. Default is False.
        max_results: Maximum number of matching lines to return. Default is 100.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        effective_max_results = _resolve_max_results(
            "grep",
            max_results,
            default=_DEFAULT_GREP_MAX_RESULTS,
            upper_bound=_MAX_GREP_MAX_RESULTS,
        )
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            if thread_data is None:
                raise SandboxRuntimeError("Thread data not available for local sandbox")
            path = _resolve_local_read_path(path, thread_data)
        matches, truncated = sandbox.grep(
            path,
            pattern,
            glob=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=effective_max_results,
        )
        if thread_data is not None:
            matches = [
                GrepMatch(
                    path=mask_local_paths_in_output(match.path, thread_data),
                    line_number=match.line_number,
                    line=match.line,
                )
                for match in matches
            ]
        return _format_grep_results(requested_path, matches, truncated)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except NotADirectoryError:
        # Self-heal instead of bouncing back an error: `grep` takes a directory root, and
        # pointing it at a single file is the most natural mistake to make (every other file
        # tool takes a file). Searching that one file is unambiguously what was meant, so do
        # it and say so, rather than making the agent spend a turn rediscovering the API.
        return _grep_single_file(
            runtime,
            requested_path,
            pattern,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"
    except PermissionError as e:
        return f"Error: Permission denied: {requested_path}{f' — {e}' if str(e) else ''}"
    except Exception as e:
        return f"Error: Unexpected error searching file contents: {_sanitize_error(e, runtime)}"


async def _grep_tool_async(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = _DEFAULT_GREP_MAX_RESULTS,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        grep_tool.func,
        runtime,
        description,
        pattern,
        path,
        glob,
        literal,
        case_sensitive,
        max_results,
    )


grep_tool.coroutine = _grep_tool_async


def read_current_file_content(runtime: Runtime | None, path: str) -> str:
    """Read the full current content of ``path`` using read_file's resolution rules.

    Shared by ``read_file_tool`` and ``ReadBeforeWriteMiddleware`` (issue #3857)
    so the gate hashes exactly the bytes the read tool would see. Raises
    ``FileNotFoundError`` when the file does not exist; other sandbox errors
    propagate to the caller.
    """
    sandbox = ensure_sandbox_initialized(runtime)
    ensure_thread_directories_exist(runtime)
    if is_local_sandbox(runtime):
        thread_data = get_thread_data(runtime)
        validate_local_tool_path(path, thread_data, read_only=True)
        if _is_skills_path(path):
            path = _resolve_skills_path(path)
        elif _is_acp_workspace_path(path):
            path = _resolve_acp_workspace_path(path, _extract_thread_id_from_thread_data(thread_data))
        elif not _is_custom_mount_path(path):
            path = _resolve_and_validate_user_data_path(path, thread_data)
        # Custom mount paths are resolved by LocalSandbox._resolve_path()
    return sandbox.read_file(path)


def _read_file_max_chars() -> int:
    """Active char cap for ``read_file`` output. Read at call time so tests can patch it."""
    try:
        from deerflow.config.app_config import get_app_config

        sandbox_cfg = get_app_config().sandbox
        return sandbox_cfg.read_file_output_max_chars if sandbox_cfg else 50000
    except Exception:
        return 50000


def _apply_line_range(content: str, start_line: int | None, end_line: int | None) -> str:
    """Return the requested line window of *content*, prefixed with the applied window.

    Half-open ranges are honoured: ``start_line`` alone means "to end of file" and
    ``end_line`` alone means "from line 1". The previous implementation required
    *both* bounds and otherwise fell through with the range silently dropped, so
    ``start_line=60`` returned the head of the file — which then got truncated to
    the same opening block the model had already seen. Session ``c2518bc7`` burned
    9 reads plus a ``bash wc -l`` on one 428-line rules file that way.

    The ``[lines A-B of T]`` prefix is emitted only for ranged reads, so whole-file
    reads stay byte-identical for ReadFileDedup / read-before-write marks.
    """
    if start_line is None and end_line is None:
        return content

    lines = content.splitlines()
    total = len(lines)
    start = 1 if start_line is None else max(1, start_line)
    end = total if end_line is None else min(total, end_line)

    if start > total:
        return f"(no such lines: start_line={start_line} is past end of file — the file has {total} lines. Request a range within 1-{total}.)"
    if end < start:
        return f"(empty range: end_line={end_line} precedes start_line={start_line} — the file has {total} lines. Request a range within 1-{total}.)"

    window = "\n".join(lines[start - 1 : end])
    return f"[lines {start}-{end} of {total}]\n{window}"


@tool("read_file", parse_docstring=True)
def read_file_tool(
    runtime: Runtime,
    description: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read the contents of a text file. Use this to examine source code, configuration files, logs, or any text-based file.

    PREFER read_file over bash cat/head/tail: read_file handles virtual path
    resolution, provides line-range selection, and detects binary files.
    Use read_file for all text file reading — only fall back to bash for
    binary file inspection or when you need text processing pipelines (awk, sed).

    Args:
        description: Explain why you are reading this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to read.
        start_line: Optional starting line number (1-indexed, inclusive). May be used without end_line to read to the end of the file.
        end_line: Optional ending line number (1-indexed, inclusive). May be used without start_line to read from the first line.
    """
    try:
        requested_path = path
        content = read_current_file_content(runtime, path)
        if not content:
            return "(empty)"
        total_lines = len(content.splitlines())
        content = _apply_line_range(content, start_line, end_line)
        return _truncate_read_file_output(content, _read_file_max_chars(), total_lines=total_lines)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied reading file: {requested_path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {requested_path}"
    except UnicodeDecodeError:
        return (
            f"Error: cannot read '{requested_path}' as text — it appears to be a binary file "
            "(e.g. .xlsx, .pdf, or an image). read_file only supports UTF-8 text. "
            "Use view_image for images, or use bash ONLY if you need to inspect raw bytes "
            "(xxd, hexdump) or process binary formats with specialized tools."
        )
    except Exception as e:
        return f"Error: Unexpected error reading file: {_sanitize_error(e, runtime)}"


async def _read_file_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(read_file_tool.func, runtime, description, path, start_line, end_line)


read_file_tool.coroutine = _read_file_tool_async


def _effective_write_file_max_bytes() -> int:
    """Return the active size cap for non-append write_file calls.

    Reads ``DEERFLOW_WRITE_FILE_MAX_BYTES`` at call time (not import time)
    so tests and runtime tweaks take effect without restart. Falls back to
    the default on missing/malformed values. A non-positive value disables
    the guard.
    """
    raw = os.environ.get(_WRITE_FILE_MAX_BYTES_ENV)
    if raw is None:
        return _WRITE_FILE_CONTENT_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return _WRITE_FILE_CONTENT_MAX_BYTES


@tool("write_file", parse_docstring=True)
def write_file_tool(
    runtime: Runtime,
    description: str,
    path: str,
    content: str,
    append: bool = False,
) -> str:
    """Write text content to a file. By default this overwrites the target file; set append=True to add content to the end without replacing existing content.

    READ-BEFORE-WRITE (issue #3857): if the target file already exists (including
    append=True), you must have read its CURRENT version with read_file first.
    Any write invalidates earlier reads, so re-read between consecutive
    modifications — a ranged read of the relevant section is enough. Writes
    that fail this check are rejected with an error.

    PREFER write_file over bash heredoc/echo: write_file handles path resolution,
    creates intermediate directories automatically, and avoids shell escaping issues.
    Always use write_file for creating/updating files — only fall back to bash for
    binary file manipulation or edge cases write_file cannot handle.

    SIZE POLICY (issue #3189):
    A single non-append write_file call must not exceed 80 KB of UTF-8 content.
    Oversized single-shot writes correlate with LLM streaming chunk-gap
    timeouts because the tool-call JSON payload — which the model must emit as
    one continuous stream — grows past the safe window. For larger documents,
    use ONE of these strategies (write_file rejects oversized payloads with an
    actionable error):

      1. INCREMENTAL EDIT (preferred for revisions): after the initial write,
         use `str_replace` to surgically update sections. This is the same
         pattern Claude Code's Write+Edit and OpenAI Codex's apply_patch use,
         and keeps each tool call's payload small.
      2. APPEND-IN-CHUNKS (for new long-form content): split the document into
         sections, each well under 80 KB. First call uses append=False to
         create the file; subsequent calls use append=True. The 80 KB cap does
         NOT apply to append=True calls.

    Operators can override the cap via env var `DEERFLOW_WRITE_FILE_MAX_BYTES`
    (0 disables the guard entirely). Raising it risks streaming timeouts.

    Args:
        description: Explain why you are writing to this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to write to. ALWAYS PROVIDE THIS PARAMETER SECOND.
        content: The content to write to the file. ALWAYS PROVIDE THIS PARAMETER THIRD.
        append: Whether to append content to the end of the file instead of overwriting it. Defaults to False.
    """
    if not append:
        max_bytes = _effective_write_file_max_bytes()
        if max_bytes > 0:
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > max_bytes:
                return (
                    f"Error: write_file content ({content_bytes} bytes) exceeds the "
                    f"{max_bytes}-byte single-call limit. Split the content into smaller "
                    "pieces: either (a) write the first section now, then use `str_replace` "
                    "for further edits, or (b) call write_file again with append=True "
                    "carrying the next section. See SIZE POLICY in the tool docstring "
                    "or issue #3189 for the rationale."
                )
    try:
        requested_path = path
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            if not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()
        with get_file_operation_lock(sandbox, path):
            sandbox.write_file(path, content, append)
        return "OK"
    except SandboxError as e:
        return _format_write_file_error(requested_path, e, runtime)
    except PermissionError:
        return _truncate_write_file_error_detail(
            f"Error: Permission denied writing to file: {requested_path}",
            _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
        )
    except IsADirectoryError:
        return _truncate_write_file_error_detail(
            f"Error: Path is a directory, not a file: {requested_path}",
            _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
        )
    except OSError as e:
        return _format_write_file_error(requested_path, e, runtime)
    except Exception as e:
        return _format_write_file_error(requested_path, e, runtime)


async def _write_file_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    content: str,
    append: bool = False,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(write_file_tool.func, runtime, description, path, content, append)


write_file_tool.coroutine = _write_file_tool_async


@tool("str_replace", parse_docstring=True)
def str_replace_tool(
    runtime: Runtime,
    description: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    """Replace a substring in a file with another substring.
    If `replace_all` is False (default), the substring to replace must appear **exactly once**
    in the file; if it occurs more than once the edit is REJECTED and nothing is written.

    READ-BEFORE-WRITE (issue #3857): you must have read the file's CURRENT
    version with read_file first; any write invalidates earlier reads.

    Args:
        description: Explain why you are replacing the substring in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to replace the substring in. ALWAYS PROVIDE THIS PARAMETER SECOND.
        old_str: The substring to replace. Must be unique in the file unless `replace_all` is True. ALWAYS PROVIDE THIS PARAMETER THIRD.
        new_str: The new substring. ALWAYS PROVIDE THIS PARAMETER FOURTH.
        replace_all: Replace every occurrence instead of requiring a unique match. Default is False.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            if not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()
        with get_file_operation_lock(sandbox, path):
            content = sandbox.read_file(path)
            if not content:
                return "OK"
            if old_str not in content:
                return f"Error: String to replace not found in file: {requested_path}"
            if replace_all:
                content = content.replace(old_str, new_str)
            else:
                # Ambiguity guard: silently editing the FIRST of several matches is how
                # "edited the wrong entry" bugs stay invisible — the file is still valid
                # and the entry count is unchanged, so structural gates cannot see it.
                occurrences = content.count(old_str)
                if occurrences > 1:
                    return (
                        f"Error: old_str is not unique in {requested_path}: found {occurrences} occurrences. "
                        "Refusing to guess which one to edit — nothing was written. "
                        "Either extend old_str with surrounding context until it matches exactly once, "
                        "or pass replace_all=True if every occurrence should change."
                    )
                content = content.replace(old_str, new_str, 1)
            sandbox.write_file(path, content)
        return "OK"
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied accessing file: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error replacing string: {_sanitize_error(e, runtime)}"


async def _str_replace_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        str_replace_tool.func,
        runtime,
        description,
        path,
        old_str,
        new_str,
        replace_all,
    )


str_replace_tool.coroutine = _str_replace_tool_async


_POINTER_OPS = ("replace", "add", "remove", "get")
_POINTER_VALUE_OPS = ("replace", "add")


def _json_pointer_tokens(pointer: str) -> list[str]:
    """Decode an RFC 6901 JSON Pointer into its tokens.

    ``~1`` is ``/`` and ``~0`` is ``~`` — needed because judgment documents are keyed by
    OCR source names that may contain slashes. Raises ``ValueError`` for a pointer that is
    not rooted, so a caller-side typo cannot be silently treated as the whole document.
    """
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/' (or be an empty string for the whole document)")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _pointer_step(container: Any, token: str, *, pointer: str) -> Any:
    """Resolve one pointer token against *container*."""
    if isinstance(container, dict):
        if token not in container:
            raise KeyError(f"{pointer} — key {token!r} does not exist")
        return container[token]
    if isinstance(container, list):
        try:
            index = int(token)
        except ValueError:
            raise KeyError(f"{pointer} — {token!r} is not a valid list index") from None
        if not -len(container) <= index < len(container):
            raise KeyError(f"{pointer} — index {index} is out of range (list has {len(container)} items)")
        return container[index]
    raise KeyError(f"{pointer} — cannot descend into a {type(container).__name__} at token {token!r}")


def _pointer_resolve_parent(doc: Any, tokens: list[str], *, pointer: str) -> Any:
    """Return the container that holds the pointer's last token."""
    node = doc
    for token in tokens[:-1]:
        node = _pointer_step(node, token, pointer=pointer)
    return node


def _apply_pointer_patch(doc: Any, patch: dict, *, label: str) -> Any:
    """Apply one pointer patch to *doc* in place; return the value for ``get``.

    Raises ``ValueError`` / ``KeyError`` with a message naming the pointer, so the caller
    can abort the whole batch without writing.
    """
    pointer = patch["pointer"]
    op = patch["op"]
    tokens = _json_pointer_tokens(pointer)
    if not tokens:
        raise ValueError(f"{label}: the whole-document pointer '' cannot be {op}d; target a specific member")

    parent = _pointer_resolve_parent(doc, tokens, pointer=pointer)
    last = tokens[-1]

    if op == "get":
        return _pointer_step(parent, last, pointer=pointer)

    if op == "remove":
        if isinstance(parent, dict):
            if last not in parent:
                raise KeyError(f"{pointer} — nothing to remove")
            del parent[last]
            return None
        if isinstance(parent, list):
            _pointer_step(parent, last, pointer=pointer)  # range check
            del parent[int(last)]
            return None
        raise KeyError(f"{pointer} — cannot remove from a {type(parent).__name__}")

    value = patch["value"]
    if op == "replace":
        # replace never creates: a missing target means the pointer is wrong, and silently
        # creating the field would let a typo look like a successful edit.
        _pointer_step(parent, last, pointer=pointer)
        if isinstance(parent, list):
            parent[int(last)] = value
        else:
            parent[last] = value
        return None

    # add: the parent must already exist (checked above by resolving it); only the final
    # member may be new. "-" appends to a list, per RFC 6901.
    if isinstance(parent, list):
        if last == "-":
            parent.append(value)
            return None
        try:
            index = int(last)
        except ValueError:
            raise KeyError(f"{pointer} — {last!r} is not a valid list index") from None
        if not 0 <= index <= len(parent):
            raise KeyError(f"{pointer} — index {index} is out of range for insertion (list has {len(parent)} items)")
        parent.insert(index, value)
        return None
    if isinstance(parent, dict):
        # `add` never overwrites. On a dict keyed by a stable identifier (criteria_parsed's
        # 四分类 is keyed by 条件ID), `parent[last] = value` would replace the WHOLE entry and
        # silently drop every field the caller did not mention — worse than the index drift
        # this keying replaced, because that at least left 同义词/原文 behind as a fingerprint
        # (thread `3a745b38`). Missing-key `add` is the create path; existing-key edits go
        # through `replace`, which targets a field and leaves siblings alone.
        if last in parent:
            raise KeyError(f"{pointer} — key {last!r} already exists; `add` only creates. To change one field use `replace` on that field (e.g. {pointer}/<field>); to swap the whole entry use `replace` on this same pointer")
        parent[last] = value
        return None
    raise KeyError(f"{pointer} — cannot add to a {type(parent).__name__}")


def _pointer_has_numeric_index(patches: list[dict] | None) -> bool:
    """True when any pointer addresses a list element by position.

    Position is not a stable address: an ``add``/``remove`` anywhere earlier in that list
    shifts every later index, so "retry the SAME patches" — correct for a dict path — sends
    the write to a *different* entry. thread `3a745b38` lost 24 single-field writes that way.
    """
    for patch in patches or []:
        if not isinstance(patch, dict):
            continue
        pointer = patch.get("pointer")
        if not isinstance(pointer, str):
            continue
        try:
            tokens = _json_pointer_tokens(pointer)
        except ValueError:
            continue
        for token in tokens:
            if token == "-":
                return True
            try:
                int(token)
            except ValueError:
                continue
            return True
    return False


def _hash_mismatch_error(requested_path: str, actual: str, form: str | None, supplied: str, patches: list[dict] | None = None) -> str:
    """The version-check rejection, written so recovery costs one call instead of three.

    The old text reported the current hash and then said "re-read it, recompute the patches
    against the new content, and retry". Observed effect (thread `93d8a2c6`, seq 1220-1224):
    the model went and ran ``sha256sum`` to recompute a value this very message had just
    handed it, then retried the same patches unchanged — three steps to recover from a
    one-step problem, in a session where every step re-sends the whole context.

    Three things are fixed here:

    * **Say the reported hash is usable.** It is the current one; quoting it back is a valid
      retry, not a guess.
    * **Make the advice form-specific.** ``old_str`` patches are matched against text, so a
      changed file can genuinely invalidate them and a re-read is required. Pointer patches
      address members by path, so they survive edits elsewhere in the file — re-reading is
      only needed when the *values* being written were derived from what was read.
    * **Do not promise that to numeric list indices.** A pointer that indexes a list by
      position is only stable while that list's length is; the blanket "your pointers are
      unaffected by edits elsewhere" was false for exactly the case that cost thread
      `3a745b38` two QC rounds. Those pointers get told to re-confirm instead.

    An empty ``supplied`` is called out separately: that is a missing argument, not a stale
    one, and telling the caller to "re-read" would send them down the wrong path.
    """
    head = f"Error: expected_hash does not match the current content of {requested_path}. Current sha256 starts with {actual[:12]}."
    if not supplied:
        return f"{head} No expected_hash was supplied — it is required. Get it with `sha256sum {requested_path} | cut -c1-12` (or reuse the value above, which is current) and retry."
    if form == "pointer":
        if _pointer_has_numeric_index(patches):
            return (
                f"{head} One or more of your pointers addresses a list element by numeric index. If that list "
                f"gained or lost entries since you read it, the same index now points to a DIFFERENT entry, so "
                f"retrying unchanged would write to the wrong place. Re-read (or send a `get`) to confirm which "
                f"entry each index holds now, then retry with expected_hash={actual[:12]}."
            )
        return (
            f"{head} Your pointers are unaffected by edits elsewhere in the file, so if the values you are writing "
            f"do not depend on what you read (for example setting a field to a value you were given), retry the SAME "
            f"patches with expected_hash={actual[:12]}. Re-read first only if a value had to be computed from the "
            f"previous content, or if a `replace` target may no longer exist."
        )
    return f"{head} The file changed since you read it, and `old_str` patches are matched against its text, so they may no longer apply. Re-read the file, rebuild the patches against the new content, then retry with the hash of that read."


def _detect_json_indent(text: str) -> int:
    """Guess the file's indent width so a rewrite does not reformat the whole document.

    A reformatted file shows up as an all-lines diff, which hides the two fields that
    actually changed and defeats any structural review of the edit.
    """
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def _classify_patch_form(patches: list) -> tuple[str | None, str | None]:
    """Return ``(form, error)`` where form is ``"text"`` or ``"pointer"``.

    Mixing the two forms in one batch is rejected on purpose: text replacement operates on
    the serialized document and pointer ops on the parsed one, so alternating them would
    require re-serializing between patches and the resulting formatting would be
    unpredictable. Two calls are clearer than one unpredictable call.
    """
    if not isinstance(patches, list) or not patches:
        return None, "Error: patches must be a non-empty list of {'old_str': ..., 'new_str': ...} or {'pointer': ..., 'op': ...} objects"
    forms = set()
    for i, patch in enumerate(patches, start=1):
        if not isinstance(patch, dict):
            return None, f"Error: patch {i} must be an object"
        if "pointer" in patch or "op" in patch:
            forms.add("pointer")
        elif "old_str" in patch or "new_str" in patch:
            forms.add("text")
        else:
            return None, f"Error: patch {i} must be either {{'old_str', 'new_str'}} (text replacement) or {{'pointer', 'op'}} (JSON object edit)"
    if len(forms) > 1:
        return None, "Error: do not mix {'old_str','new_str'} and {'pointer','op'} patches in one call — text replacement and object edits apply to different representations. Split them into two calls."
    return forms.pop(), None


def _validate_pointer_patches(patches: list) -> str | None:
    """Return an error message for the first invalid pointer patch, or ``None``."""
    for i, patch in enumerate(patches, start=1):
        pointer = patch.get("pointer")
        op = patch.get("op")
        if not isinstance(pointer, str):
            return f"Error: patch {i} needs a string 'pointer' (RFC 6901, e.g. /documents/rec/judgments/IN-1/conclusion)"
        if op not in _POINTER_OPS:
            return f"Error: patch {i} has an unsupported op {op!r}; supported ops are {', '.join(_POINTER_OPS)}"
        if op in _POINTER_VALUE_OPS and "value" not in patch:
            return f"Error: patch {i} ({op}) requires a 'value'"
        try:
            _json_pointer_tokens(pointer)
        except ValueError as exc:
            return f"Error: patch {i} — {exc}"
    return None


@tool("apply_json_patches", parse_docstring=True)
def apply_json_patches_tool(
    runtime: Runtime,
    description: str,
    path: str,
    expected_hash: str,
    patches: list[dict],
) -> str:
    """Apply several edits to one file atomically, under a single version check.

    Use this instead of a chain of `str_replace` calls when you already know every edit a
    file needs — for example applying a whole QC report's findings to one judgment JSON.
    A chain of single edits forces a full re-read between each one (every write
    invalidates the previous read), so N edits cost N whole-file reads plus N writes;
    this costs one read and one write.

    TWO PATCH FORMS. Pick one per call; mixing them in a single call is rejected.

    1. Text replacement — `{"old_str": ..., "new_str": ...}`. Works on any file. Each
       `old_str` must appear EXACTLY ONCE at the time it is applied (same rule as
       `str_replace`); ambiguous matches are rejected rather than guessed. Patches apply
       in order to the accumulating text, so a later patch sees earlier edits.

    2. JSON object edit — `{"pointer": ..., "op": ..., "value": ...}`. Requires the file to
       be valid JSON. `pointer` is an RFC 6901 JSON Pointer
       (`/documents/{source}/judgments/IN-1/conclusion`; `~1` means `/`, `~0` means `~`;
       numeric tokens index arrays and `-` appends). `op` is one of:
         * `replace` — overwrite an EXISTING member (never creates; a missing target means
           the pointer is wrong)
         * `add`     — create a NEW member, or insert into a list. On an object, a key that
           already exists is REJECTED: `add` would replace the whole entry and drop every
           field you did not mention. Change one field with `replace` on that field.
         * `remove`  — delete an existing member
         * `get`     — read one member without writing anything
       Prefer this form for judgment/criteria JSON: one conclusion change usually has to
       move `reason`, `evidence` and `exclusion_triggered` with it, and doing that by
       string match is how "changed the reason, forgot the conclusion" happens. `get` also
       lets you re-check one entry without re-reading the whole file.

    ADDRESS BY KEY, NOT BY POSITION. Object keys are stable addresses: `/judgments/IN-1/` and
    `/四分类/排除_可从病例获取/EX-12-1/` hit that entry or fail loudly, no matter what was added
    or removed elsewhere. A numeric list index is NOT a stable address — it stays in range
    and keeps succeeding while pointing at a different element, so a wrong write leaves no
    trace. When a collection's entries carry a stable identifier, address them by that key.
    ⛔ If you must use a numeric index, re-confirm it after anything changes that list's
    length; an `add` shifts every later index by one, across calls as well as within a batch.

    ATOMIC: if any patch does not apply, nothing is written at all. A half-applied
    structured document is worse than an unapplied one, because the file stays
    syntactically valid and entry counts stay correct, so structural checks cannot see
    that only some edits landed.

    VERSION CHECKED: `expected_hash` must match the file's current content (full sha256
    hex, or its first 12 characters). This is the only defence against applying edits
    computed from a stale read. Note what it does NOT check: it answers "has anyone else
    changed this file", not "does my pointer still address what I meant". A stale numeric
    index passes the hash check and writes to the wrong entry.

    HOW TO GET `expected_hash` — no file tool returns it, so obtain it explicitly:

        bash: sha256sum <path> | cut -c1-12

    Run that immediately before this call, AFTER any write of your own has landed.
    ⛔ Never guess a hash and never reuse one from earlier in the conversation: every write
    changes it, and a fabricated value costs a rejected call plus a recovery round-trip.
    On mismatch the tool reports the actual hash, and the error tells you whether you can
    retry with it directly or have to re-read first.

    Object edits keep the file's existing indent and do not escape non-ASCII, so the diff
    shows only the members you changed.

    Args:
        description: Explain why you are editing the file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to patch. ALWAYS PROVIDE THIS PARAMETER SECOND.
        expected_hash: sha256 of the file's current content (full hex or 12-char prefix). ALWAYS PROVIDE THIS PARAMETER THIRD.
        patches: List of `{"old_str", "new_str"}` OR of `{"pointer", "op", "value"}`, applied in order. ALWAYS PROVIDE THIS PARAMETER FOURTH.
    """
    import hashlib

    form, form_error = _classify_patch_form(patches)
    if form_error is not None:
        return form_error

    if form == "pointer":
        pointer_error = _validate_pointer_patches(patches)
        if pointer_error is not None:
            return pointer_error
    else:
        for i, patch in enumerate(patches, start=1):
            if "old_str" not in patch or "new_str" not in patch:
                return f"Error: patch {i} must be an object with both 'old_str' and 'new_str'"
            if not isinstance(patch["old_str"], str) or not patch["old_str"]:
                return f"Error: patch {i} has an empty old_str; an empty match would insert between every character"
            if not isinstance(patch["new_str"], str):
                return f"Error: patch {i} has a non-string new_str"

    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            if not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()

        # One lock for the whole batch: read, verify, patch and write must not interleave
        # with another writer, or the version check would guard nothing.
        with get_file_operation_lock(sandbox, path):
            content = sandbox.read_file(path)
            actual = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
            supplied = (expected_hash or "").strip().lower()
            if not supplied or not (actual == supplied or actual.startswith(supplied)):
                return _hash_mismatch_error(requested_path, actual, form, supplied, patches)

            if form == "pointer":
                try:
                    document = json.loads(content or "")
                except json.JSONDecodeError as exc:
                    return f"Error: {requested_path} is not valid JSON ({exc}), so object edits cannot be applied. Nothing was written. Use the {{'old_str','new_str'}} form for non-JSON files, or fix the file first."

                gets: list[str] = []
                mutated = False
                for i, patch in enumerate(patches, start=1):
                    label = f"patch {i} of {len(patches)}"
                    try:
                        got = _apply_pointer_patch(document, patch, label=label)
                    except (KeyError, ValueError, TypeError) as exc:
                        detail = exc.args[0] if exc.args else str(exc)
                        return f"Error: {label} did not apply — {detail}. Nothing was written."
                    if patch["op"] == "get":
                        rendered = json.dumps(got, ensure_ascii=False)
                        gets.append(f"{patch['pointer']} = {rendered[:2000]}")
                    else:
                        mutated = True

                if not mutated:
                    # A read-only batch must not touch the file: `get` exists so one entry
                    # can be re-checked without a whole-file read, not as a write path.
                    joined = "\n".join(gets)
                    return f"OK: read {len(gets)} pointer(s) from {requested_path}; nothing written.\n{joined}"

                patched = json.dumps(document, ensure_ascii=False, indent=_detect_json_indent(content or ""))
                sandbox.write_file(path, patched)
                new_hash = hashlib.sha256(patched.encode("utf-8")).hexdigest()
                summary = f"OK: applied {len(patches)} patch(es) to {requested_path} in one write. New content sha256 starts with {new_hash[:12]}."
                return f"{summary}\n" + "\n".join(gets) if gets else summary

            patched = content
            for i, patch in enumerate(patches, start=1):
                old_str, new_str = patch["old_str"], patch["new_str"]
                occurrences = patched.count(old_str)
                if occurrences == 0:
                    return (
                        f"Error: patch {i} of {len(patches)} did not apply — old_str not found in {requested_path}. "
                        "Nothing was written. Note that earlier patches in this batch may have changed the text a "
                        "later patch expected; check the ordering."
                    )
                if occurrences > 1:
                    return f"Error: patch {i} of {len(patches)} is ambiguous — old_str occurs {occurrences} times in {requested_path}. Nothing was written. Extend old_str with surrounding context until it matches exactly once."
                patched = patched.replace(old_str, new_str, 1)

            sandbox.write_file(path, patched)
            new_hash = hashlib.sha256(patched.encode("utf-8")).hexdigest()
        return f"OK: applied {len(patches)} patch(es) to {requested_path} in one write. New content sha256 starts with {new_hash[:12]}."
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied accessing file: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error applying patches: {_sanitize_error(e, runtime)}"


async def _apply_json_patches_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    expected_hash: str,
    patches: list[dict],
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        apply_json_patches_tool.func,
        runtime,
        description,
        path,
        expected_hash,
        patches,
    )


apply_json_patches_tool.coroutine = _apply_json_patches_tool_async
