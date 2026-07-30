"""AgentRunSandbox — DeerFlow Sandbox implementation wrapping agentrun-sdk."""

from __future__ import annotations

import base64
import binascii
import errno
import logging
import os
import shlex
import tempfile
import threading

from agentrun.utils.exception import ClientError, ResourceNotExistError, ServerError

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, should_ignore_path, truncate_line

logger = logging.getLogger(__name__)


class AgentRunSandbox(Sandbox):
    """DeerFlow Sandbox wrapping an agentrun-sdk Sandbox instance.

    All methods delegate to the agentrun-sdk Sandbox object.
    A threading.Lock serialises access because the SDK client may not be
    safe for concurrent use from multiple threads.

    Note: agentrun-sdk (v0.0.36) returns plain dicts from most operations,
    not typed objects.
    """

    def __init__(self, id: str, sdk_sandbox, command_timeout: int = 600) -> None:
        super().__init__(id)
        self._sdk = sdk_sandbox
        self._lock = threading.Lock()
        self._command_timeout = command_timeout

    def execute_command(self, command: str) -> str:
        with self._lock:
            result = self._sdk.process.cmd(
                command=command,
                cwd="/home/user",
                timeout=self._command_timeout,
            )
            inner = result.get("result", {}) if isinstance(result, dict) else {}
            output = inner.get("stdout", "") or ""
            stderr = inner.get("stderr", "")
            if stderr:
                output = f"{output}\n{stderr}" if output else stderr
            return output

    def read_file(self, path: str) -> str:
        result = self._sdk.file.read(path)
        if isinstance(result, dict):
            return result.get("content", "") or ""
        return result

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        if append:
            with self._lock:
                try:
                    existing = self.read_file(path)
                    content = existing + content
                except Exception:
                    pass
                self._sdk.file.write(path=path, content=content, create_dir=True)
        else:
            with self._lock:
                self._sdk.file.write(path=path, content=content, create_dir=True)

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        result = self._sdk.file_system.list(path=path, depth=max_depth)
        if isinstance(result, dict):
            entries = result.get("entries") or []
        else:
            entries = getattr(result, "entries", None) or []
        return [e.get("name", "") if isinstance(e, dict) else e.name for e in entries]

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        cmd = f"find {shlex.quote(path)} -name {shlex.quote(pattern)} -maxdepth 5"
        if not include_dirs:
            cmd += " -type f"
        cmd += f" | head -n {max_results + 1}"

        output = self.execute_command(cmd)
        lines = [line for line in output.strip().split("\n") if line and not should_ignore_path(line)]
        truncated = len(lines) > max_results
        return lines[:max_results], truncated

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        flags = []
        if not case_sensitive:
            flags.append("-i")
        if literal:
            flags.append("-F")
        flags.append("-rn")
        if glob:
            flags.append(f"--include={shlex.quote(glob)}")

        flag_str = " ".join(flags)
        cmd = f"grep {flag_str} {shlex.quote(pattern)} {shlex.quote(path)} | head -n {max_results + 1}"

        output = self.execute_command(cmd)
        matches: list[GrepMatch] = []
        for line in output.strip().split("\n"):
            if not line or should_ignore_path(line):
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path, line_no, content = parts[0], parts[1], parts[2]
                matches.append(
                    GrepMatch(
                        path=file_path,
                        line_number=int(line_no) if line_no.isdigit() else 0,
                        line=truncate_line(content),
                    )
                )

        truncated = len(matches) > max_results
        return matches[:max_results], truncated

    def download_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        """Download binary content from the AgentRun sandbox.

        Args:
            path: Absolute virtual path (must be under VIRTUAL_PATH_PREFIX).
            max_bytes: Optional caller-imposed size cap (post-download
                enforcement; SDK currently does not expose stat/streaming).

        Raises:
            PermissionError: path traversal or outside VIRTUAL_PATH_PREFIX.
            FileNotFoundError: file does not exist in sandbox (mapped from
                ResourceNotExistError / ClientError(404)).
            OSError(errno=EFBIG): file exceeds cap.
            OSError: other SDK errors (server, transport, etc.).
            TypeError: SDK returned non-bytes for binary content
                (refused rather than silently corrupted via encode).
        """
        # (1) Path traversal defense (parity with AioSandbox).
        normalised = path.replace("\\", "/")
        for segment in normalised.split("/"):
            if segment == "..":
                raise PermissionError(f"Access denied: path traversal detected in '{path}'")

        # (2) Virtual-path whitelist (parity with AioSandbox).
        stripped_path = normalised.lstrip("/")
        allowed_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            raise PermissionError(f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}': '{path}'")

        # (3) SDK call with exception mapping.
        try:
            result = self._sdk.file.read(path)
        except ResourceNotExistError:
            raise FileNotFoundError(errno.ENOENT, f"File not found in sandbox: '{path}'", path) from None
        except ClientError as e:
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                raise FileNotFoundError(errno.ENOENT, f"File not found in sandbox: '{path}'", path) from None
            if status_code == 403:
                raise PermissionError(f"Access denied by sandbox: '{path}'") from None
            raise OSError(f"Sandbox client error downloading '{path}': {e}") from e
        except ServerError as e:
            raise OSError(f"Sandbox server error downloading '{path}': {e}") from e
        except Exception as e:
            raise OSError(f"Failed to download file '{path}' from sandbox: {e}") from e

        # (4) Extract bytes — SDK returns dict{"content": <str|bytes>, "encoding": <str>}.
        # For binary files SDK base64-encodes content and sets encoding="base64".
        # For text files SDK returns plain str (encoding usually "utf-8" or empty).
        # Decode according to `encoding`; refuse unknown encodings rather than corrupting data.
        raw_bytes: bytes
        if isinstance(result, dict):
            content = result.get("content", b"")
            if isinstance(content, bytes):
                raw_bytes = content
            elif isinstance(content, str):
                encoding = (result.get("encoding") or "").lower()
                if encoding == "base64":
                    try:
                        raw_bytes = base64.b64decode(content, validate=False)
                    except (binascii.Error, ValueError) as e:
                        raise OSError(f"AgentRun SDK returned invalid base64 content for '{path}': {e}") from e
                elif encoding in ("", "utf-8", "utf8", "ascii"):
                    # Plain text file — keep prior behavior for read_file()-style callers.
                    raw_bytes = content.encode("utf-8")
                else:
                    raise TypeError(f"AgentRun SDK returned content with unsupported encoding '{encoding}' for '{path}'; cannot safely materialize bytes.")
            else:
                raise TypeError(f"Unexpected content type {type(content).__name__} for '{path}'")
        elif isinstance(result, bytes):
            raw_bytes = result
        else:
            raise TypeError(f"AgentRun SDK returned {type(result).__name__} for '{path}'; expected bytes or dict")

        # (5) Post-download size cap (SDK does not expose stat/streaming yet).
        builtin_cap = 100 * 1024 * 1024  # aligned with LocalSandbox builtin cap
        effective_cap = min(builtin_cap, max_bytes) if max_bytes is not None else builtin_cap
        if len(raw_bytes) > effective_cap:
            raise OSError(errno.EFBIG, f"File exceeds maximum download size of {effective_cap} bytes", path)
        return raw_bytes

    def update_file(self, path: str, content: bytes) -> None:
        """Upload binary content to a sandbox path.

        The agentrun SDK exposes text writes as ``file.write(path, content, ...)``
        (str only, sanitizes as UTF-8), and binary uploads via
        ``file_system.upload(local_file_path=..., target_file_path=...)``.
        Do NOT call ``self._sdk.file.upload`` — that attribute does not exist
        and was the source of the ``AttributeError: 'FileOperations' object
        has no attribute 'upload'`` that hit md_to_docx before this fix.
        """
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            with self._lock:
                self._sdk.file_system.upload(
                    local_file_path=tmp_path,
                    target_file_path=path,
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
