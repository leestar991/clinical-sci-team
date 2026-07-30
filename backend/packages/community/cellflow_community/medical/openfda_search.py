"""OpenFDA search tool centered on the Drugs@FDA endpoint.

This module provides a LangChain-compatible tool for searching FDA approval and
product metadata via OpenFDA, with Drugs@FDA application records as the primary
dataset and supplementary product fields when available.

No API key required - uses the free public OpenFDA API.
"""

import asyncio
import logging
import urllib.parse
from typing import Any

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from cellflow_community.medical.rate_limiter import AsyncDelayRateLimiter
from cellflow_community.medical.utils import FindingsType, create_finding, generate_source_id
from cellflow_community.research import source_ledger as _ledger
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

# OpenFDA API endpoint for FDA approvals / product metadata
OPENFDA_URL = "https://api.fda.gov/drug/drugsfda.json"

# OpenFDA allows 240 requests/minute without an API key (4 req/sec).
_fda_rate_limiter = AsyncDelayRateLimiter(requests_per_second=3.5)


def sanitize_query_value(value: str | None) -> str | None:
    """Sanitize query value for OpenFDA API.

    Args:
        value: Raw query value

    Returns:
        Sanitized value with proper quoting
    """
    if value and " " in value and not value.startswith('"'):
        return f'"{value}"'
    return value


def build_openfda_url(
    route: str | None = None,
    brand_name: str | None = None,
    generic_name: str | None = None,
    dosage_form: str | None = None,
    marketing_status: str | None = None,
    sponsor_name: str | None = None,
    application_number: str | None = None,
    substance_name: str | None = None,
    limit: int = 100,
) -> str:
    """Build a Drugs@FDA search URL with supported parameters.

    Args:
        route: Route of administration
        brand_name: Brand name of drug
        generic_name: Generic name of drug
        dosage_form: Dosage form of the product
        marketing_status: Marketing status such as Prescription or Over-the-counter
        sponsor_name: Sponsoring company name
        application_number: NDA/ANDA/BLA application number
        substance_name: Active substance name
        limit: Maximum number of results

    Returns:
        Formatted URL string
    """
    search_terms: list[str] = []

    if brand_name:
        search_terms.append(f"openfda.brand_name:{sanitize_query_value(brand_name)}")
    if generic_name:
        search_terms.append(f"openfda.generic_name:{sanitize_query_value(generic_name)}")
    if route:
        search_terms.append(f"products.route:{sanitize_query_value(route)}")
    if dosage_form:
        search_terms.append(f"products.dosage_form:{sanitize_query_value(dosage_form)}")
    if marketing_status:
        search_terms.append(f"products.marketing_status:{sanitize_query_value(marketing_status)}")
    if sponsor_name:
        search_terms.append(f"sponsor_name:{sanitize_query_value(sponsor_name)}")
    if application_number:
        search_terms.append(f"application_number:{sanitize_query_value(application_number)}")
    if substance_name:
        search_terms.append(f"openfda.substance_name:{sanitize_query_value(substance_name)}")

    if not search_terms:
        params = {"limit": limit}
    else:
        params = {"search": "+AND+".join(search_terms), "limit": limit}

    query_string = urllib.parse.urlencode(params)
    # The '+AND+' must literally equal +AND+ in the string, bypass urlencode replacing + with %2B
    query_string = query_string.replace("%2BAND%2B", "+AND+")
    return f"{OPENFDA_URL}?{query_string}"


async def query_openfda(
    route: str | None = None,
    brand_name: str | None = None,
    generic_name: str | None = None,
    dosage_form: str | None = None,
    marketing_status: str | None = None,
    sponsor_name: str | None = None,
    application_number: str | None = None,
    substance_name: str | None = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """Query the Drugs@FDA OpenFDA API.

    Args:
        route: Route of administration
        brand_name: Brand name of drug
        generic_name: Generic name of drug
        dosage_form: Dosage form of the product
        marketing_status: Marketing status such as Prescription or Over-the-counter
        sponsor_name: Sponsoring company name
        application_number: NDA/ANDA/BLA application number
        substance_name: Active substance name
        limit: Maximum number of results

    Returns:
        Tuple of (drug result dictionaries, total result count)
    """
    request_url = build_openfda_url(
        route=route,
        brand_name=brand_name,
        generic_name=generic_name,
        dosage_form=dosage_form,
        marketing_status=marketing_status,
        sponsor_name=sponsor_name,
        application_number=application_number,
        substance_name=substance_name,
        limit=limit,
    )

    # A per-call client (context-managed) is created fresh on the current running
    # loop and closed on every exit path — no module-level singleton to leak or to
    # bind to one event loop (subagents run on their own loops). One client covers
    # the retry attempts of this single call.
    max_retries = 3
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            await _fda_rate_limiter.acquire()

            try:
                response = await client.get(request_url, timeout=30.0)

                if response.status_code == 404:
                    # OpenFDA returns 404 when no results match — not an error
                    logger.info("OpenFDA returned no results (404)")
                    return [], 0

                if response.status_code == 429:
                    raise httpx.HTTPStatusError(f"429 Too Many Requests (attempt {attempt + 1})", request=response.request, response=response)

                response.raise_for_status()
                payload = response.json()
                results = payload.get("results", [])
                total_matches = payload.get("meta", {}).get("results", {}).get("total", len(results))
                logger.info(f"OpenFDA returned {len(results)} results")
                return results, total_matches

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch from OpenFDA after {max_retries} attempts: {e}")
                    raise RuntimeError(f"OpenFDA request failed: {e}") from e
                await asyncio.sleep(2**attempt + 1)

    raise RuntimeError("OpenFDA request failed")


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving their original order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        clean_value = value.strip()
        if clean_value and clean_value not in seen:
            deduped.append(clean_value)
            seen.add(clean_value)
    return deduped


def _format_fda_date(value: str) -> str:
    """Format YYYYMMDD dates into YYYY-MM-DD when possible."""
    if value and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _normalize_fda_url(url: str) -> str:
    """Normalize common FDA document URLs to https."""
    return url.replace("http://www.accessdata.fda.gov", "https://www.accessdata.fda.gov")


def _humanize_submission_status(status: str) -> str:
    """Convert compact Drugs@FDA submission status codes into readable text."""
    return {
        "AP": "Approved",
        "TA": "Tentative approval",
        "WD": "Withdrawn",
    }.get(status, status)


def _extract_product_fields(drug_item: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized Drugs@FDA product fields from a raw entry."""
    openfda = drug_item.get("openfda", {})
    products = drug_item.get("products", [])

    brand_names = _dedupe_preserve_order([product.get("brand_name", "") for product in products if product.get("brand_name")] + openfda.get("brand_name", []))
    generic_names = _dedupe_preserve_order(openfda.get("generic_name", []))
    manufacturers = _dedupe_preserve_order(openfda.get("manufacturer_name", []))
    routes = _dedupe_preserve_order([product.get("route", "") for product in products if product.get("route")] + openfda.get("route", []))
    dosage_forms = _dedupe_preserve_order([product.get("dosage_form", "") for product in products if product.get("dosage_form")])
    marketing_statuses = _dedupe_preserve_order([product.get("marketing_status", "") for product in products if product.get("marketing_status")])
    product_types = _dedupe_preserve_order(openfda.get("product_type", []))
    substance_names = _dedupe_preserve_order(openfda.get("substance_name", []))

    active_ingredients: list[dict[str, str]] = []
    seen_ingredients: set[tuple] = set()
    for product in products:
        for ingredient in product.get("active_ingredients", []):
            name = str(ingredient.get("name", "")).strip()
            strength = str(ingredient.get("strength", "")).strip()
            ingredient_key = (name, strength)
            if name and ingredient_key not in seen_ingredients:
                active_ingredients.append({"name": name, "strength": strength})
                seen_ingredients.add(ingredient_key)

    return {
        "brand_names": brand_names,
        "generic_names": generic_names,
        "manufacturers": manufacturers,
        "routes": routes,
        "dosage_forms": dosage_forms,
        "marketing_statuses": marketing_statuses,
        "product_types": product_types,
        "substance_names": substance_names,
        "active_ingredients": active_ingredients,
    }


def _get_latest_submission(submissions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the most recent submission by submission_status_date."""
    if not submissions:
        return {}
    return max(submissions, key=lambda submission: submission.get("submission_status_date", ""))


def _get_latest_doc_url(submissions: list[dict[str, Any]], doc_type: str) -> str:
    """Return the latest application doc URL for a given document type."""
    latest_date = ""
    latest_url = ""
    for submission in submissions:
        for doc in submission.get("application_docs", []):
            if doc.get("type") != doc_type or not doc.get("url"):
                continue
            doc_date = str(doc.get("date", "") or submission.get("submission_status_date", ""))
            if doc_date >= latest_date:
                latest_date = doc_date
                latest_url = _normalize_fda_url(doc["url"])
    return latest_url


def _build_application_url(application_number: str) -> str:
    """Build a human-friendly Drugs@FDA application overview URL."""
    digits_only = "".join(ch for ch in application_number if ch.isdigit())
    if digits_only:
        return f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={digits_only}"
    return "https://www.accessdata.fda.gov/scripts/cder/daf/"


def summarize_drugs(fda_results: list[dict[str, Any]], total_matches: int | None = None) -> dict[str, Any]:
    """Summarize Drugs@FDA metadata from search results.

    Args:
        fda_results: Raw results from OpenFDA API

    Returns:
        Summary dictionary with application and route breakdown
    """
    unique_applications: set[str] = set()
    unique_drugs: set[str] = set()
    route_counts: dict[str, int] = {}
    sponsor_counts: dict[str, int] = {}

    for item in fda_results:
        application_number = item.get("application_number", "")
        if application_number:
            unique_applications.add(application_number)

        extracted = _extract_product_fields(item)
        for brand in extracted["brand_names"]:
            unique_drugs.add(brand)
        for route in extracted["routes"]:
            route_counts[route] = route_counts.get(route, 0) + 1

        sponsor = item.get("sponsor_name", "")
        if sponsor:
            sponsor_counts[sponsor] = sponsor_counts.get(sponsor, 0) + 1

    return {
        "total_applications": total_matches if total_matches is not None else (len(unique_applications) or len(fda_results)),
        "routes": route_counts,
        "drug_names": list(unique_drugs)[:20],
        "top_sponsors": dict(sorted(sponsor_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
    }


def format_drug_detail(drug_item: dict[str, Any]) -> dict[str, Any]:
    """Format a single Drugs@FDA item into a compact finding.

    Args:
        drug_item: Raw drug item from OpenFDA

    Returns:
        Standardized finding dictionary
    """
    extracted = _extract_product_fields(drug_item)
    submissions = drug_item.get("submissions", [])
    application_number = str(drug_item.get("application_number", "")).strip()
    sponsor_name = str(drug_item.get("sponsor_name", "")).strip()

    title_base = ""
    if extracted["brand_names"]:
        title_base = extracted["brand_names"][0]
        if len(extracted["brand_names"]) > 1:
            title_base = f"{title_base} +{len(extracted['brand_names']) - 1}"
    elif extracted["generic_names"]:
        title_base = extracted["generic_names"][0]
    elif application_number:
        title_base = "Drug Application"
    else:
        title_base = "Drug Record"

    title = f"{title_base} ({application_number})" if application_number else title_base

    latest_submission = _get_latest_submission(submissions)
    latest_submission_date = _format_fda_date(latest_submission.get("submission_status_date", ""))
    latest_submission_status = _humanize_submission_status(latest_submission.get("submission_status", ""))
    latest_submission_class = latest_submission.get("submission_class_code_description", "")

    latest_label_url = _get_latest_doc_url(submissions, "Label")
    latest_letter_url = _get_latest_doc_url(submissions, "Letter")
    latest_review_url = _get_latest_doc_url(submissions, "Review")
    source = latest_label_url or latest_letter_url or latest_review_url or _build_application_url(application_number)

    active_ingredient_parts = []
    for ingredient in extracted["active_ingredients"][:4]:
        if ingredient["strength"]:
            active_ingredient_parts.append(f"{ingredient['name']} {ingredient['strength']}")
        else:
            active_ingredient_parts.append(ingredient["name"])

    text_parts: list[str] = []
    if sponsor_name:
        text_parts.append(f"Sponsor: {sponsor_name}.")
    if extracted["generic_names"]:
        text_parts.append(f"Generic: {', '.join(extracted['generic_names'][:3])}.")
    if active_ingredient_parts:
        text_parts.append(f"Active ingredients: {'; '.join(active_ingredient_parts)}.")

    product_parts = []
    if extracted["dosage_forms"]:
        product_parts.append(f"dosage forms {', '.join(extracted['dosage_forms'][:3])}")
    if extracted["routes"]:
        product_parts.append(f"routes {', '.join(extracted['routes'][:3])}")
    if extracted["marketing_statuses"]:
        product_parts.append(f"marketing status {', '.join(extracted['marketing_statuses'][:3])}")
    if product_parts:
        text_parts.append(f"Products: {'; '.join(product_parts)}.")

    if latest_submission_class or latest_submission_status or latest_submission_date:
        update_bits = []
        if latest_submission_class:
            update_bits.append(latest_submission_class)
        if latest_submission_status:
            update_bits.append(latest_submission_status)
        update_text = " / ".join(update_bits) if update_bits else "update"
        if latest_submission_date:
            text_parts.append(f"Latest FDA update: {update_text} on {latest_submission_date}.")
        else:
            text_parts.append(f"Latest FDA update: {update_text}.")

    available_docs = []
    if latest_label_url:
        available_docs.append("label")
    if latest_letter_url:
        available_docs.append("letter")
    if latest_review_url:
        available_docs.append("review")
    if available_docs:
        text_parts.append(f"Docs available: {', '.join(available_docs)}.")

    summary_text = " ".join(text_parts).strip()

    return create_finding(
        title=title,
        source=source,
        text=summary_text,
        url=source,
        findings_type=FindingsType.OPENFDA,
        application_number=application_number,
        brand_names=extracted["brand_names"],
        generic_names=extracted["generic_names"],
        manufacturers=extracted["manufacturers"],
        routes=extracted["routes"],
        dosage_forms=extracted["dosage_forms"],
        marketing_statuses=extracted["marketing_statuses"],
        active_ingredients=extracted["active_ingredients"],
        sponsor_name=sponsor_name,
        latest_label_url=latest_label_url,
        latest_letter_url=latest_letter_url,
        latest_review_url=latest_review_url,
    )


OPENFDA_SEARCH_DESCRIPTION = (
    "Search OpenFDA Drugs@FDA for a drug's FDA REGULATORY status — approval, application number "
    "(NDA/BLA/ANDA), sponsor, marketing status, active ingredient, dosage form, route. USE for "
    "'is X approved / by whom / what is its application number' questions. NOT for efficacy/safety "
    "evidence (use pubmed_search) or trial results (use clinical_trials_search); does NOT return "
    "full label text. "
    "Search by brand OR generic name — not both at once; one drug per call. `marketing_status` is "
    "one of {Prescription, Over-the-counter, Discontinued}. "
    "Returns per record: application number, sponsor, approval/marketing status, and product fields. "
    "If a brand name returns nothing, retry with the generic name before concluding it is unapproved. "
    "No API key required."
)


@tool(description=OPENFDA_SEARCH_DESCRIPTION, response_format="content_and_artifact")
async def openfda_search(
    topic: str,
    brand_name: str | None = None,
    generic_name: str | None = None,
    route: str | None = None,
    dosage_form: str | None = None,
    marketing_status: str | None = None,
    sponsor_name: str | None = None,
    application_number: str | None = None,
    substance_name: str | None = None,
    runtime: Runtime = None,
    config: RunnableConfig = None,
) -> tuple:
    """Search OpenFDA drug records centered on Drugs@FDA metadata.

    The number of returned entries is controlled by runtime configuration.

    Args:
        brand_name: Brand name of the drug (e.g., 'Advil', 'Lipitor')
        generic_name: Generic name of the drug (e.g., 'ibuprofen', 'atorvastatin')
        route: Route of administration (e.g., 'ORAL', 'TOPICAL', 'INJECTION')
        dosage_form: Dosage form of the product (e.g., 'TABLET', 'SUSPENSION')
        marketing_status: Marketing status (e.g., 'Prescription', 'Over-the-counter')
        sponsor_name: FDA application sponsor name
        application_number: NDA/ANDA/BLA application number
        substance_name: Active substance name
        topic: A simple sentence explaining why you are calling this tool and what you want to find. CRITICAL: This MUST be in the SAME language as the user's original query.

    Returns:
        Tuple of (formatted_text, artifact_dict) where artifact contains source_registry
    """
    # Check that at least one parameter is provided
    if not any(
        [
            brand_name,
            generic_name,
            route,
            dosage_form,
            marketing_status,
            sponsor_name,
            application_number,
            substance_name,
        ]
    ):
        return (
            "Please provide at least one search parameter (brand_name, generic_name, route, dosage_form, marketing_status, sponsor_name, application_number, or substance_name)",
            {"source_registry": {}},
        )

    query_params = {
        "brand_name": brand_name,
        "generic_name": generic_name,
        "route": route,
        "dosage_form": dosage_form,
        "marketing_status": marketing_status,
        "sponsor_name": sponsor_name,
        "application_number": application_number,
        "substance_name": substance_name,
    }

    max_results = 20

    try:
        results, total_matches = await query_openfda(
            route=route,
            brand_name=brand_name,
            generic_name=generic_name,
            dosage_form=dosage_form,
            marketing_status=marketing_status,
            sponsor_name=sponsor_name,
            application_number=application_number,
            substance_name=substance_name,
            limit=max_results,
        )
    except Exception as e:
        logger.error(f"OpenFDA search error: {e}", exc_info=True)
        return (f"No Drugs@FDA records were returned because the OpenFDA request failed: {e}", {"source_registry": {}})

    if not results:
        return (f"No Drugs@FDA records found matching the criteria: {query_params}", {"source_registry": {}})

    # Generate summary
    summary = summarize_drugs(results, total_matches=total_matches)

    # Format individual drug findings (limit to max_results)
    findings = []
    for drug_item in results[:max_results]:
        finding = format_drug_detail(drug_item)
        findings.append(finding)

    # Build output with summary and details
    output_parts = [
        "# OpenFDA Search Results (Drugs@FDA)\n",
        f"**Total Applications Found:** {summary['total_applications']}\n",
    ]

    if summary["routes"]:
        output_parts.append("\n**Routes in Shown Results:**\n")
        for route_name, count in list(summary["routes"].items())[:5]:
            output_parts.append(f"- {route_name}: {count} applications\n")

    if summary["drug_names"]:
        output_parts.append(f"\n**Sample Drug Names:** {', '.join(summary['drug_names'][:10])}\n")
    if summary["top_sponsors"]:
        output_parts.append("\n**Sponsors in Shown Results:**\n")
        for sponsor, count in list(summary["top_sponsors"].items())[:5]:
            output_parts.append(f"- {sponsor}: {count} applications\n")

    # Get researcher_idx from config metadata for unique prefixing
    researcher_idx = None
    if config and isinstance(config, dict):
        researcher_idx = config.get("metadata", {}).get("researcher_idx")

    source_registry = {}
    for finding in findings:
        # Generate a stable ID based on title or generic name
        title = finding.get("title", "unknown")
        src_id = generate_source_id(base_name=f"fda_{title}", source_registry=source_registry, researcher_idx=str(researcher_idx) if researcher_idx is not None else None, max_words=4)

        source_registry[src_id] = {"url": finding.get("url", ""), "title": finding.get("title", "Drug Application"), "summary": finding.get("text", "")}

        output_parts.append(f"\n--- SOURCE |[{src_id}]|: {finding.get('title', 'Drug Application')} ---\n")
        output_parts.append(f"URL: {finding.get('url', '')}\n\n")
        output_parts.append(f"SUMMARY:\n{finding.get('text', '')}\n")

    formatted_content = "".join(output_parts)

    # Record each FDA record to the citation ledger and append a [cite:N] source block
    # (replaces the legacy snake-case source_registry JSON trailer, which was dead data).
    formatted_content += await _ledger.arecord_registry(runtime, source_registry, tool_name="openfda_search", action="search")

    return (formatted_content, {"source_registry": source_registry})
