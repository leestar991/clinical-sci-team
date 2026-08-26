"""Configuration for the inline-bash artifact-write policy.

Structured artifacts have dedicated tools: ``write_file`` for a first write and
``apply_json_patches`` for edits. Both are observable (the payload is in the transcript),
both are covered by the read-before-write gate, and ``apply_json_patches`` validates the
document. An inline ``python3 -c`` / heredoc that rewrites the same file bypasses all
three — and the model reaches for it exactly when the file is already broken.

Measured on thread ``88df83a8``: the IN judgment task rewrote
``judgments_draft_MCRC-2150006_IN.json`` twice through ``python3 << 'EOF'`` heredocs
(steps 861 and 863) to patch quoting, while its own delegation prompt said in rule 3:
"产物只能由 write_file(首次落盘)或 apply_json_patches(改判)写。禁止用 bash 内联脚本
(python3 -c、heredoc、echo >)生成或改写 .json". Prose alone did not hold.

This lives in a middleware rather than in ``sandbox/tools.py``'s bash validation because
those validators only run for the LOCAL sandbox; AIO/container deployments would not be
covered.

Default **off**, and deliberately narrow: it fires only when inline code (or a
redirection) and a governed artifact path and a write intent are ALL present. A guard that
also blocks ``python3 skill_script.py --out x.json`` would break the main path it is
supposed to protect.
"""

from typing import Literal

from pydantic import BaseModel, Field

BashWritePolicyMode = Literal["warn", "block"]


class BashWritePolicyConfig(BaseModel):
    """Refuse inline-bash writes to structured artifacts."""

    enabled: bool = Field(
        default=False,
        description="Enforce the inline-bash artifact-write policy for the bash tool",
    )
    mode: BashWritePolicyMode = Field(
        default="block",
        description="block = refuse the command and name the right tool; warn = run it but append the guidance",
    )
    blocked_suffixes: list[str] = Field(
        default_factory=lambda: [".json"],
        description="File suffixes treated as structured artifacts that only write_file / apply_json_patches may write",
    )
    governed_prefix: str = Field(
        default="/mnt/user-data/",
        description="Only artifacts under this sandbox prefix are governed (plus relative paths, since the sandbox working directory lives under it). Absolute paths elsewhere — /tmp scratch, read-only /mnt/skills — are left alone.",
    )
