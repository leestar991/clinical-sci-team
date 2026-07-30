"""Clinical trials search tool for ClinicalTrials.gov.

This module provides LangChain-compatible tools for searching clinical trials
using the ClinicalTrials.gov API v2.

No API key required - uses the free public ClinicalTrials.gov API.
"""

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from cellflow_community.medical.rate_limiter import AsyncDelayRateLimiter
from cellflow_community.medical.utils import (
    FindingsType,
    create_finding,
    format_search_results,
    generate_source_id,
)
from cellflow_community.research import source_ledger as _ledger
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

# ClinicalTrials.gov API v2 endpoint
BASE_URL = "https://clinicaltrials.gov/api/v2/studies"

# Mapping input parameters to ClinicalTrials.gov API v2 query keys
QUERY_MAP: dict[str, str] = {
    "condition": "query.cond",
    "location": "query.locn",
    "title": "query.titles",
    "intervention": "query.intr",
    "outcome": "query.outc",
    "sponsor": "query.spons",
    "lead_sponsor": "query.lead",
    "study_id": "query.id",
    "patient": "query.patient",
    "status": "filter.overallStatus",
}

# Global rate limiter for clinical trials
# 45 requests per 60 seconds = 0.75 req/sec
_ct_rate_limiter = AsyncDelayRateLimiter(requests_per_second=0.75)

# Guidance returned when clinicaltrials.gov blocks/errors — points the agent at
# working alternatives instead of retrying a request that will keep failing.
CT_BLOCKED_GUIDANCE = "clinicaltrials.gov 暂不可用(被来源方拦截或异常),请改用 pubmed_search / web_search 获取试验信息。"


async def _curl_get_json(url: str, params: dict[str, Any], timeout: float = 30.0) -> tuple[int, Any]:
    """用系统 curl 子进程发 GET(绕过 clinicaltrials.gov 对 httpx TLS 指纹的 WAF 拦截)。

    返回 (http_status, parsed_json | None)。非阻塞(asyncio subprocess);绝不走 shell —
    参数以数组形式传 exec,query 经 urlencode 编码后拼进 URL,防命令注入。
    """
    full_url = f"{url}?{urlencode(params)}" if params else url
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sS",
        "--max-time",
        str(int(timeout)),
        "-H",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-w",
        "\n%{http_code}",
        full_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace")
    body_str, _, status_str = text.rpartition("\n")
    try:
        status = int(status_str.strip())
    except ValueError:
        return (0, None)
    if status != 200:
        return (status, None)
    try:
        return (200, json.loads(body_str))
    except json.JSONDecodeError:
        return (200, None)


async def search_clinical_trials(query_fields: dict[str, str] | None = None, page_size: int = 10, max_pages: int = 1, fields: list[str] | None = None) -> list[dict[str, Any]] | str:
    """Search ClinicalTrials.gov for clinical trials.

    Args:
        query_fields: Dictionary of search parameters
        page_size: Number of results per page
        max_pages: Maximum number of pages to fetch
        fields: List of fields to include in response

    Returns:
        List of clinical trial dictionaries, or a guidance string (fast-return,
        no retry) when clinicaltrials.gov blocks/errors the request — see
        CT_BLOCKED_GUIDANCE. Callers must check for a str before iterating.
    """
    query_fields = query_fields or {}
    fields = fields or ["NCTId", "BriefTitle", "OverallStatus", "InterventionName", "PrimaryOutcomeMeasure"]

    params: dict[str, Any] = {"format": "json", "pageSize": page_size, "fields": ",".join(fields)}

    # Map input fields to API query parameters
    for key, value in query_fields.items():
        if key in QUERY_MAP and value:
            param_key = QUERY_MAP[key]
            params[param_key] = value.strip()

    results: list[dict[str, Any]] = []
    next_token: str | None = None
    page = 0

    while page < max_pages:
        if next_token:
            params["pageToken"] = next_token

        await _ct_rate_limiter.acquire()
        status, data = await _curl_get_json(BASE_URL, params, timeout=30.0)
        if status != 200 or data is None:
            logger.warning("ClinicalTrials 检索失败/被拦截 (status=%s)", status)
            return CT_BLOCKED_GUIDANCE
        page_results = data.get("studies", [])
        results.extend(page_results)
        next_token = data.get("nextPageToken")

        if not next_token:
            break

        page += 1

    return results


def format_clinical_trial(trial: dict[str, Any]) -> dict[str, Any]:
    """Format clinical trial data to standardized finding format.

    Args:
        trial: Raw clinical trial data from API

    Returns:
        Standardized finding dictionary
    """
    try:
        protocol_section = trial.get("protocolSection", {})

        # Extract identification info
        identification = protocol_section.get("identificationModule", {})
        nct_id = identification.get("nctId") or trial.get("NCTId", "")
        title = identification.get("briefTitle") or trial.get("BriefTitle", "")

        # Extract status info
        status_module = protocol_section.get("statusModule", {})
        overall_status = status_module.get("overallStatus") or trial.get("OverallStatus", "")
        start_date = status_module.get("startDateStruct", {}).get("date", "")
        completion_date = status_module.get("completionDateStruct", {}).get("date", "")

        # Extract description
        description_module = protocol_section.get("descriptionModule", {})
        brief_summary = description_module.get("briefSummary", "")

        # Extract sponsor info
        sponsor_module = protocol_section.get("sponsorCollaboratorsModule", {})
        lead_sponsor = sponsor_module.get("leadSponsor", {}).get("name", "")

        # Extract phase
        design_module = protocol_section.get("designModule", {})
        phases = design_module.get("phases", [])
        phase = ", ".join(phases) if phases else trial.get("Phase", "Not specified")

        # Extract conditions
        conditions_module = protocol_section.get("conditionsModule", {})
        conditions = conditions_module.get("conditions", [])

        # Extract interventions
        arms_module = protocol_section.get("armsInterventionsModule", {})
        interventions = arms_module.get("interventions", [])
        intervention_names = [i.get("name", "") for i in interventions if i.get("name")]

        # Build text content
        text_parts = [f"Title: {title}"]
        if brief_summary:
            text_parts.append(f"Summary: {brief_summary[:500]}...")
        if overall_status:
            text_parts.append(f"Status: {overall_status}")
        if phase:
            text_parts.append(f"Phase: {phase}")
        if conditions:
            text_parts.append(f"Conditions: {', '.join(conditions[:5])}")
        if intervention_names:
            text_parts.append(f"Interventions: {', '.join(intervention_names[:5])}")
        if lead_sponsor:
            text_parts.append(f"Sponsor: {lead_sponsor}")
        if start_date:
            text_parts.append(f"Start Date: {start_date}")
        if completion_date:
            text_parts.append(f"Completion Date: {completion_date}")

        # Extract Results data if available
        results_section = trial.get("resultsSection", {})
        if results_section:
            outcomes_module = results_section.get("outcomeMeasuresModule", {})
            outcomes = outcomes_module.get("outcomeMeasures", [])

            if outcomes:
                text_parts.append("\n--- Study Results & Outcomes ---")
                for outcome in outcomes[:3]:  # Top 3 outcomes to save tokens
                    title = outcome.get("title", "Outcome")
                    desc = outcome.get("description", "")
                    # Extract the first denominator or general count if possible, but mainly the description holds the conclusion metadata
                    text_parts.append(f"* {title}")
                    if desc:
                        # Truncate very long descriptions
                        text_parts.append(f"  {desc[:300]}..." if len(desc) > 300 else f"  {desc}")

                    # Sometimes researchers put notes in population description
                    pop_desc = outcome.get("populationDescription", "")
                    if pop_desc:
                        text_parts.append(f"  Note: {pop_desc[:200]}")

        source = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""

        return create_finding(
            title=title or f"Clinical Trial {nct_id}",
            source=source,
            text="\n".join(text_parts),
            url=source,
            findings_type=FindingsType.CLINICAL_TRIAL,
            nct_id=nct_id,
            status=overall_status,
            phase=phase,
            conditions=conditions,
            interventions=intervention_names,
            sponsor=lead_sponsor,
            raw_data=trial,
        )

    except Exception as e:
        logger.error(f"Error formatting clinical trial: {e}")
        return create_finding(
            title="Clinical Trial (Parse Error)",
            source="",
            text=json.dumps(trial, indent=2, ensure_ascii=False)[:1000],
            findings_type=FindingsType.CLINICAL_TRIAL,
        )


CLINICAL_TRIALS_SEARCH_DESCRIPTION = (
    "Search ClinicalTrials.gov to FIND trials by condition / intervention / sponsor / status / "
    "location. USE to discover which trials exist and their phase and status. This returns a LIST "
    "only — it does NOT include enrollment N, endpoints, doses, or results; for those you MUST "
    "follow up with get_trial_details(nct_id) on the 1–3 most relevant NCT IDs. "
    "Use ONE condition and/or ONE intervention per call, and the GENERIC drug name (not brand); "
    "`status` is one of {recruiting, active, completed, terminated, withdrawn, not_yet_recruiting, "
    "unknown}. "
    "Returns per trial: NCT ID, title, status, phase, conditions, interventions. "
    "If a brand name returns nothing, retry with the generic / INN name before concluding no trials "
    "exist. No API key required."
)

GET_TRIAL_DETAILS_DESCRIPTION = (
    "Fetch the FULL protocol + results for ONE trial by NCT ID. USE this AFTER "
    "clinical_trials_search to extract enrollment N, arms/doses, primary/secondary endpoints, "
    "results, and adverse events — clinical_trials_search does NOT return these. "
    "Pass exactly one NCT ID like 'NCT03600883'. "
    "Returns RAW sanitized ClinicalTrials.gov JSON (not a summary): read the fenced JSON and look in "
    "protocolSection.designModule, outcomesModule, resultsSection, adverseEventsModule; skip empty "
    "sections. If resultsSection is absent, results are NOT yet posted — say so, do not infer "
    "outcomes from the trial design. No API key required."
)


@tool(description=CLINICAL_TRIALS_SEARCH_DESCRIPTION, response_format="content_and_artifact")
async def clinical_trials_search(
    topic: str, condition: str | None = None, intervention: str | None = None, location: str | None = None, sponsor: str | None = None, status: str | None = None, max_results: int = 10, runtime: Runtime = None, config: RunnableConfig = None
) -> tuple:
    """Search ClinicalTrials.gov for clinical trials.

    Args:
        condition: Medical condition or disease to search for (e.g., 'diabetes', 'breast cancer')
        intervention: Treatment or intervention type (e.g., 'drug', 'surgery')
        location: Geographic location of trials (e.g., 'United States', 'China')
        sponsor: Organization sponsoring the trial
        status: Trial status filter (e.g., 'RECRUITING', 'COMPLETED')
        max_results: Maximum number of results to return
        topic: A simple sentence explaining why you are calling this tool and what you want to find. CRITICAL: This MUST be in the SAME language as the user's original query.
        config: Runtime configuration

    Returns:
        Tuple of (formatted_text, artifact_dict) where artifact contains source_registry
    """
    # Build query fields from parameters
    query_fields = {}
    if condition:
        query_fields["condition"] = condition
    if intervention:
        query_fields["intervention"] = intervention
    if location:
        query_fields["location"] = location
    if sponsor:
        query_fields["sponsor"] = sponsor
    if status:
        query_fields["status"] = status

    if not query_fields:
        return ("Please provide at least one search parameter (condition, intervention, location, or sponsor)", {"source_registry": {}})

    # Define fields to fetch
    fields = ["NCTId", "BriefTitle", "OverallStatus", "InterventionName", "Phase", "StartDate", "CompletionDate", "LeadSponsorName", "BriefSummary", "ResultsSection"]

    try:
        results = await search_clinical_trials(query_fields=query_fields, page_size=min(max_results, 20), max_pages=1, fields=fields)
    except Exception as e:
        logger.error(f"Clinical trials search error: {e}", exc_info=True)
        return (f"No clinical trials were returned because the ClinicalTrials.gov request failed: {e}", {"source_registry": {}})

    # search_clinical_trials fast-returns a guidance string (no retry) when
    # clinicaltrials.gov blocks/errors the request instead of a list of trials.
    # Surface it directly to the agent — do NOT fall through to the list
    # comprehension below, which would silently iterate the string's characters.
    if isinstance(results, str):
        return (results, {"source_registry": {}})

    if not results:
        return (f"No clinical trials found matching the criteria: {query_fields}", {"source_registry": {}})

    # Format results
    findings = [format_clinical_trial(trial) for trial in results]
    formatted_output = format_search_results(findings, "ClinicalTrials.gov")

    # Get researcher_idx from config metadata for unique prefixing
    researcher_idx = None
    if config and isinstance(config, dict):
        researcher_idx = config.get("metadata", {}).get("researcher_idx")

    source_registry = {}
    for finding in findings:
        nct_id = finding.get("nct_id")
        if not nct_id:
            continue
        src_id = generate_source_id(base_name=finding["title"], source_registry=source_registry, researcher_idx=str(researcher_idx) if researcher_idx is not None else None, max_words=4)

        source_registry[src_id] = {
            "url": finding["url"],
            "title": finding["title"],
            "summary": finding["text"],
            "nct_id": nct_id,
        }

    # Record each trial to the citation ledger and append a [cite:N] source block
    # (replaces the legacy snake-case source_registry JSON trailer, which was dead data).
    formatted_output += await _ledger.arecord_registry(runtime, source_registry, tool_name="clinical_trials_search", action="search")

    return (formatted_output, {"source_registry": source_registry})


async def get_study_details_api(nct_id: str) -> dict[str, Any] | None:
    """Fetch raw study details directly from ClinicalTrials.gov API API v2.

    Args:
        nct_id: The NCT ID (e.g., 'NCT01234567')

    Returns:
        Raw study JSON data or None if not found/error
    """
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    await _ct_rate_limiter.acquire()
    status, data = await _curl_get_json(url, {}, timeout=30.0)
    if status == 404:
        logger.info(f"No trial found for NCT ID {nct_id}")
        return None
    if status != 200 or data is None:
        raise RuntimeError(f"ClinicalTrials.gov trial details request failed for {nct_id} (status={status})")
    return data


@tool(description=GET_TRIAL_DETAILS_DESCRIPTION)
async def get_trial_details(nct_id: str, topic: str, runtime: Runtime = None, config: RunnableConfig = None) -> tuple:
    """Get detailed information about a specific clinical trial.

    Args:
        nct_id: The NCT ID of the clinical trial (e.g., 'NCT12345678')
        topic: A simple sentence explaining why you are calling this tool and what you want to find. CRITICAL: This MUST be in the SAME language as the user's original query.
        config: Runtime configuration
    """
    # Validate NCT ID format
    if not nct_id or not nct_id.upper().startswith("NCT"):
        return (f"Invalid NCT ID format: '{nct_id}'. Please provide a valid NCT ID (e.g., 'NCT12345678')", {"source_registry": {}})

    nct_id = nct_id.upper().strip()

    try:
        trial_data = await get_study_details_api(nct_id)
    except Exception as e:
        logger.error(f"Error fetching trial details: {e}", exc_info=True)
        return (f"No trial details were returned because the ClinicalTrials.gov request failed for {nct_id}: {e}", {"source_registry": {}})

    if not trial_data:
        return (f"No trial found with NCT ID: {nct_id}", {"source_registry": {}})

    # Sanitize data to save tokens before feeding to LLM
    protocol = trial_data.get("protocolSection", {})
    if "referencesModule" in protocol:
        del protocol["referencesModule"]
    if "contactsLocationsModule" in protocol:
        del protocol["contactsLocationsModule"]
    if "ipdSharingStatementModule" in protocol:
        del protocol["ipdSharingStatementModule"]

    raw_detailed_text = json.dumps(trial_data, ensure_ascii=False, indent=2)

    # Return sanitized raw JSON — deerflow's lead agent reads and summarizes it
    # directly (see MEDICAL_DEEP_RESEARCHER_DESIGN.md §port-notes for why pharmaid's
    # in-tool LLM summarization was removed). Truncate to guard against a runaway
    # payload; ClinicalTrials.gov responses are typically well under this cap once
    # references/contacts/IPD modules are stripped above.
    trimmed = raw_detailed_text[:100000]
    summarized_content = f"# Clinical Trial: {nct_id}\n\n```json\n{trimmed}\n```"

    url = f"https://clinicaltrials.gov/study/{nct_id}"

    # Get researcher_idx from config metadata for unique prefixing
    researcher_idx = None
    if config and isinstance(config, dict):
        researcher_idx = config.get("metadata", {}).get("researcher_idx")

    # Fetch original title if available, else generic
    title = trial_data.get("protocolSection", {}).get("identificationModule", {}).get("briefTitle", f"Clinical Trial {nct_id}")

    # The user requested that the trial details result updates its corresponding entry in source registry
    # inserted by the ct search tool call. The ct search tool maps NCTIds to their title.
    src_id = generate_source_id(base_name=title, source_registry=None, researcher_idx=str(researcher_idx) if researcher_idx is not None else None, max_words=4)

    source_registry = {
        src_id: {
            "url": url,
            "title": f"NCT: {nct_id} - {title}",
            "summary": summarized_content[:8000],  # Limit summary size slightly, but keeping it large
            "nct_id": nct_id,
        }
    }

    # Record the trial to the citation ledger and append a [cite:N] source block.
    formatted_output = summarized_content + await _ledger.arecord_registry(runtime, source_registry, tool_name="get_trial_details", action="open")

    return (formatted_output, {"source_registry": source_registry})
