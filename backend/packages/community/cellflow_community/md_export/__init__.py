"""Markdown → other-format export tools.

Currently ships `md_to_docx`. Future `md_to_pdf` / `md_to_xlsx` / `md_to_pptx`
should live alongside as sibling modules with the same signature shape (source
basename + target basename, both resolved under `/mnt/user-data/outputs/`).

See docs/proposal/DOCUSIGHT_AGENT_DESIGN.md §7.4 for the port rationale and
signature contract.
"""

from cellflow_community.md_export.md_to_docx import md_to_docx

__all__ = ["md_to_docx"]
