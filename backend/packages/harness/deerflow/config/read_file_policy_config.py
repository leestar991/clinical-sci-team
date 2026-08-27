"""Configuration for the whole-file re-read policy.

Distinct from :mod:`deerflow.config.read_dedup_config`, which suppresses a repeat read of
the *same version and same range*. This one targets the case dedup structurally cannot
catch: reading the **whole** of a large file again and again, each time returning content
that is legitimately "new" to the cache (different range, or a changed file) while
re-inheriting tens of thousands of tokens per turn.

Measured on thread ``88df83a8`` (EX judgment task ``call_01_FPXHRbeulZxpejOZGPFs0023``):
``ocr_records.md`` (7,604 lines) was read 6 times, ``whole_file_read_calls=10``,
``range_overlap_lines=936``, ``tokens_before`` reached 99,755 — 1.66x the 60k compaction
trigger. Four compactions inside one task followed, and the task then lost its working
state and abandoned its assignment. The delegation template already said "read each input
at most once"; prose did not hold, so this makes it mechanical.

Default **off**: turning a successful read into an error changes agent behaviour, so it is
opted into per deployment.
"""

from typing import Literal

from pydantic import BaseModel, Field

ReadFilePolicyMode = Literal["warn", "block"]


class ReadFilePolicyConfig(BaseModel):
    """Bound repeated whole-file reads of large files within one task."""

    enabled: bool = Field(
        default=False,
        description="Enforce the whole-file re-read policy for read_file",
    )
    mode: ReadFilePolicyMode = Field(
        default="block",
        description="block = refuse the repeat whole-file read with an actionable alternative; warn = return the content but append the same guidance. Use warn to measure before enforcing.",
    )
    min_lines_for_ranged: int = Field(
        default=1500,
        ge=1,
        description="Only files whose first whole-file read was at least this many lines are governed. Small files are cheaper to re-read than to navigate, and blocking them would only add turns.",
    )
    max_entries: int = Field(
        default=5000,
        ge=1,
        description="Bounded per-(task, path) bookkeeping; oldest entries are evicted first",
    )
