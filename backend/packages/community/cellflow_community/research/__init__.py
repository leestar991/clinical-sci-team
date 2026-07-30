"""Research tools with source-ledger recording.

Same-named recorded variants of the web / medical search & fetch tools that, in
addition to their normal behavior, record every retrieved source to a per-thread
sandbox-visible ledger (`/mnt/user-data/workspace/sources.txt`) and hand the
model a stable integer `id` per source so it can cite with ``[cite:N]``.

The ledger is the source of truth read by ``resolve_citations.py`` (bundled in the
``docusight-report`` skill) when it deterministically replaces citation tags and
builds the References section.

See ``backend/docs/proposal/DOCUSIGHT_CITATION_DESIGN.md``.
"""
