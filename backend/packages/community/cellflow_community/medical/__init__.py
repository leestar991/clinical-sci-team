"""Medical search tools for deep-research agents.

Ported from pharmaid-deep_research (`open_deep_research.tools.medical`). The
tools themselves are LangChain `@tool` decorated async functions with
`response_format="content_and_artifact"`. See
`docs/proposal/MEDICAL_DEEP_RESEARCHER_DESIGN.md` for the port rationale
(deleted pharmaid-private `Configuration` + `create_chat_model` deps; the
tools now return sanitized raw JSON and let the lead agent summarize).

APIs used are public — no keys required:
- PubMed: NCBI E-utilities
- ClinicalTrials.gov: v2 API
- OpenFDA: Drugs@FDA
"""

from cellflow_community.medical.clinical_trials import (
    clinical_trials_search,
    get_trial_details,
)
from cellflow_community.medical.openfda_search import openfda_search
from cellflow_community.medical.pubmed_search import pubmed_search

__all__ = [
    "clinical_trials_search",
    "get_trial_details",
    "openfda_search",
    "pubmed_search",
]
