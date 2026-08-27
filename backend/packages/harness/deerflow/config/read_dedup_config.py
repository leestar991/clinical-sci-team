"""Configuration for version-aware ``read_file`` deduplication (criteria-token-saving Task 5).

Separate from :mod:`deerflow.config.read_before_write_config`: that one gates WRITES on
having read the current version, this one suppresses redundant READ payloads. They share
the same ``sha256`` content-hash strategy so a file's identity means the same thing to
both, and to ``cellflow_community.textin.artifacts`` (``sha256(bytes)[:12]``).

Default **off**. Returning a short reference instead of file content changes what the
model sees, so it must be opted into per deployment after a replay comparison — not
switched on silently by upgrading.
"""

from pydantic import BaseModel, Field


class ReadFileDedupConfig(BaseModel):
    """Suppress re-sending identical file content within one run.

    A subagent that reads the same ``SKILL.md`` or judgment JSON on several turns pays
    the full token cost every time. When enabled, a repeat read of the same
    ``(path, range)`` whose content hash is unchanged returns a short reference instead
    of the body.

    Correctness rule: the cache key includes the content hash, so ANY modification is a
    natural miss. A stale hit would be far worse than the tokens it saves — the agent
    would edit a file based on content that no longer exists.
    """

    enabled: bool = Field(
        default=False,
        description="Return a short reference instead of file content on a repeat read of the same version",
    )
    max_entries: int = Field(
        default=5000,
        ge=1,
        description="Bounded cache size; oldest entries are evicted first",
    )
    min_chars: int = Field(
        default=2000,
        ge=0,
        description="Only dedupe reads at least this large — short reads cost less than the indirection",
    )


class SearchDedupConfig(BaseModel):
    """Placeholder for grep/glob result deduplication.

    Declared so ``search_dedup`` in an existing ``config.yaml`` validates instead of
    being silently swallowed by ``AppConfig``'s ``extra="allow"``, which is how the
    previous placeholder went unnoticed while nothing implemented it.
    """

    enabled: bool = Field(default=False, description="Not implemented yet; declared so the key validates")
