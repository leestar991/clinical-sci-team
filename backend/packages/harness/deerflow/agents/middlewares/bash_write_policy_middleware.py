"""Refuse inline-bash writes to structured artifacts (``.json`` by default).

Structured artifacts have dedicated tools: ``write_file`` for the first write,
``apply_json_patches`` for edits. Both put the payload in the transcript, both are covered
by the read-before-write gate, and the patch tool validates the document. An inline
``python3 -c`` or heredoc that rewrites the same file bypasses all three — and the model
reaches for it precisely when the artifact is already broken, i.e. when an unobservable
rewrite is most dangerous.

Real failure (thread ``88df83a8``, IN judgment task): two ``python3 << 'EOF'`` heredocs
(steps 861, 863) rewrote ``judgments_draft_MCRC-2150006_IN.json`` to patch quoting, while
the task's own prompt forbade exactly that. The rule existed; only the enforcement was
missing.

Placed in a middleware rather than in ``sandbox/tools.py``'s bash validation because those
validators run only for the local sandbox — an AIO/container deployment would not be
covered.

**Detection is deliberately conservative**: a command is refused only when all three hold.

1. *Inline authorship or a redirection* — ``python -c``, a heredoc, ``>``/``>>``, ``tee``,
   or ``sed -i``. Running a script **file** (``python3 judge_pack.py --out x.json``) is not
   inline authorship and is never touched: that is the main path artifacts are produced by.
2. *A governed artifact path* — a token ending in a blocked suffix that is under the
   governed prefix, or relative (the sandbox working directory lives under that prefix, so
   ``cd /mnt/user-data/workspace && python3 -c "...open('a.json','w')..."`` must not slip
   through on a missing prefix). Absolute paths elsewhere (``/tmp`` scratch, read-only
   ``/mnt/skills``) are left alone.
3. *Write intent* — for inline code, an actual write call (``open(..., 'w')``,
   ``json.dump``, ``.write(``, ``write_text``, ...); a ``json.load`` probe is fine). For a
   redirection / ``tee`` / ``sed -i`` the operator itself **is** the write.

Widening any of the three would hit the main path. That trade is the whole design.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.bash_write_policy_config import BashWritePolicyConfig

logger = logging.getLogger(__name__)

_BASH_TOOLS = frozenset({"bash"})

# Inline authorship: code written into the command itself rather than invoked from a file.
_INLINE_CODE = (
    re.compile(r"\b(?:python|python3|node|ruby|perl|php)\s+(?:-\S+\s+)*-c\b"),  # -c "..."
    re.compile(r"<<-?\s*['\"]?\w+['\"]?"),  # heredoc, any tag (EOF / PYEOF / 'JSON')
    re.compile(r"\b(?:python|python3|node)\s+-\s*$", re.MULTILINE),  # `python3 -` (stdin)
)

# Write intent inside inline code. A read-only probe (json.load / readlines) has none of
# these, which is what keeps step 855/859-style inspection commands working.
#
# ⚠️ Shell redirection is deliberately NOT listed here even though it is a write: it is
# handled by ``_redirect_targets``, which checks that the redirect *target* is a governed
# artifact. Treating a bare ``>`` as inline write intent matched ``2>&1`` and ``| head``,
# which is how the read-only probes of steps 855/859 were first misclassified as writes.
_INLINE_WRITE_INTENT = re.compile(
    r"open\s*\([^)]*['\"][wax]b?\+?['\"]|json\.dump\b|\.write\s*\(|\.writelines\s*\(|write_text\s*\(|write_bytes\s*\(|shutil\.(?:copy|move)\b|os\.replace\b|\.to_json\s*\(|os\.(?:remove|unlink)\b",
)

_REDIRECT_WRITE = re.compile(r">>?\s*(?P<path>[^\s|;&<>]+)")
_TEE_WRITE = re.compile(r"\btee\s+(?:-\S+\s+)*(?P<path>[^\s|;&<>]+)")
_SED_IN_PLACE = re.compile(r"\bsed\b[^|;&]*\s-i\b")
# Deleting or renaming a governed artifact is the other escape hatch: ``rm`` removes the
# read-before-write mark along with the file, so the follow-up ``write_file`` sails through
# (thread ``881e7ba8``: a redo task `rm -f`-ed a correct split parse output, then rebuilt a
# degraded placeholder version with zero gate resistance). Governed artifacts have no
# legitimate bash lifecycle: rewrite-in-place is already covered above, removal belongs to
# apply_json_patches / an explicit user decision.
_REMOVE_OR_RENAME = re.compile(r"\b(?:rm|unlink|shred|mv)\b")
# Any token that looks like a file path; suffix filtering happens against the config.
_PATH_TOKEN = re.compile(r"[\w./~@+-]*[\w.]+")


class BashWritePolicyMiddleware(AgentMiddleware):
    """Refuse (or flag) inline-bash writes to structured artifacts."""

    def __init__(self, config: BashWritePolicyConfig | None = None) -> None:
        super().__init__()
        self._config = config or BashWritePolicyConfig()

    # -- plumbing --------------------------------------------------------

    def _command(self, request: ToolCallRequest) -> str | None:
        args = request.tool_call.get("args")
        if isinstance(args, dict):
            command = args.get("command")
            return command if isinstance(command, str) and command.strip() else None
        # step_events replaces oversized args with a truncated JSON *string*; inspecting it
        # as text still finds the inline-write shape, and a false "no command" would be a
        # silent hole exactly on the biggest commands.
        return args if isinstance(args, str) and args.strip() else None

    def _is_governed_artifact(self, token: str) -> bool:
        cleaned = token.strip().strip("'\"),;")
        if not any(cleaned.endswith(suffix) for suffix in self._config.blocked_suffixes):
            return False
        if cleaned.startswith(self._config.governed_prefix):
            return True
        # Relative path: the sandbox working directory is under the governed prefix, so a
        # `cd <prefix>/... && python3 -c "...open('a.json','w')..."` writes an artifact.
        return not cleaned.startswith("/")

    def _governed_paths(self, command: str) -> list[str]:
        return [token for token in _PATH_TOKEN.findall(command) if self._is_governed_artifact(token)]

    def _redirect_targets(self, command: str) -> list[str]:
        targets = [match.group("path") for match in _REDIRECT_WRITE.finditer(command)]
        targets.extend(match.group("path") for match in _TEE_WRITE.finditer(command))
        return [t for t in targets if self._is_governed_artifact(t)]

    @staticmethod
    def _has_inline_code(command: str) -> bool:
        return any(pattern.search(command) for pattern in _INLINE_CODE)

    def _violation(self, command: str) -> str | None:
        """Return the offending artifact path, or ``None`` when the command is fine."""
        # (a) redirection / tee straight into an artifact — the operator IS the write.
        redirected = self._redirect_targets(command)
        if redirected:
            return redirected[0]

        # (a') deleting / renaming a governed artifact — removal is the other way to
        # bypass every gate (the file's read-mark goes away with the file).
        if _REMOVE_OR_RENAME.search(command):
            governed = self._governed_paths(command)
            if governed:
                return governed[0]

        # (b) in-place stream edit of an artifact.
        if _SED_IN_PLACE.search(command):
            governed = self._governed_paths(command)
            if governed:
                return governed[0]

        # (c) inline code that both mentions an artifact and writes something.
        if self._has_inline_code(command):
            governed = self._governed_paths(command)
            if governed and _INLINE_WRITE_INTENT.search(command):
                return governed[0]
        return None

    def _guidance(self, path: str) -> str:
        return (
            f"Writing {path} from an inline bash script is not allowed. Structured artifacts must be written "
            "with write_file (first write) or apply_json_patches (edits): those go through the transcript and "
            "the read-before-write gate, and apply_json_patches validates the document — an inline rewrite does "
            'none of that. To inspect a value without rewriting, use apply_json_patches with {"op": "get", '
            '"pointer": "/..."}. If write_file was rejected for a stale version, read_file the current content '
            "first and write again; do not switch to bash to get around it. Deleting or renaming a governed "
            "artifact (rm/mv) is refused for the same reason — it destroys the read-before-write mark along "
            "with the file, so a rebuild meets no gate at all. To redo work, revise the existing artifact; "
            "to retire it, ask the user. Running a skill script that produces "
            "the file itself (python3 <script>.py --out ...) is still fine."
        )

    def _refusal(self, request: ToolCallRequest, path: str) -> ToolMessage:
        logger.info("bash write policy: blocked inline write to %s", path)
        return ToolMessage(
            content="Error: " + self._guidance(path),
            tool_call_id=request.tool_call.get("id") or "",
            name="bash",
            additional_kwargs={"bash_write_policy": "blocked"},
        )

    def _annotate(self, result: ToolMessage | Command, path: str) -> ToolMessage | Command:
        if not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            return result
        logger.info("bash write policy: flagged inline write to %s", path)
        return ToolMessage(
            content=f"{result.content}\n\n[bash write policy] {self._guidance(path)}",
            tool_call_id=result.tool_call_id,
            name=result.name,
            additional_kwargs={**(result.additional_kwargs or {}), "bash_write_policy": "warned"},
        )

    def _offending_path(self, request: ToolCallRequest) -> str | None:
        if not self._config.enabled:
            return None
        if request.tool_call.get("name") not in _BASH_TOOLS:
            return None
        command = self._command(request)
        if command is None:
            return None
        return self._violation(command)

    # -- core ------------------------------------------------------------

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        path = self._offending_path(request)
        if path is None:
            return handler(request)
        if self._config.mode == "block":
            return self._refusal(request, path)
        return self._annotate(handler(request), path)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        path = self._offending_path(request)
        if path is None:
            return await handler(request)
        if self._config.mode == "block":
            return self._refusal(request, path)
        return self._annotate(await handler(request), path)


def build_bash_write_policy_middleware(config: BashWritePolicyConfig | None = None) -> BashWritePolicyMiddleware | None:
    """Return the middleware only when enabled, so a disabled deployment pays nothing."""
    if config is None or not config.enabled:
        return None
    return BashWritePolicyMiddleware(config=config)
