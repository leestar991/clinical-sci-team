"""Utility functions for medical search tools.

This module provides common utilities for managing research findings,
deduplication, and formatting.
"""

import json
import re
from enum import StrEnum
from hashlib import md5
from typing import Any


class FindingsType(StrEnum):
    """Types of research findings from different sources."""

    PUBMED = "pubmed"
    GOOGLE = "google"
    CLINICAL_TRIAL = "clinical_trial"
    OPENFDA = "openfda"
    DRUG_INFO = "openfda"
    KEGG = "kegg"


def create_finding(title: str, source: str, text: str = "", raw_data: dict[str, Any] | None = None, url: str = "", findings_type: FindingsType = FindingsType.PUBMED, **extra_fields) -> dict[str, Any]:
    """Create a standardized finding dictionary.

    Args:
        title: Title of the finding
        source: Source URL or identifier
        text: Main text content
        raw_data: Optional raw data dictionary
        url: Optional URL (defaults to source if not provided)
        findings_type: Type of finding
        **extra_fields: Additional fields to include

    Returns:
        Standardized finding dictionary
    """
    finding = {
        "title": title,
        "source": source,
        "text": text if text else json.dumps(raw_data, ensure_ascii=False) if raw_data else "",
        "url": url or source,
        "findings_type": findings_type.value if isinstance(findings_type, FindingsType) else findings_type,
    }

    # Add raw_data if provided
    if raw_data:
        finding["raw_data"] = raw_data

    # Add any extra fields
    finding.update(extra_fields)

    return finding


def get_finding_hash(finding: dict[str, Any]) -> str:
    """Generate a hash for a finding based on its source.

    Args:
        finding: Finding dictionary

    Returns:
        MD5 hash string of the finding's source
    """
    source = finding.get("source", "") or finding.get("url", "")
    return md5(source.encode()).hexdigest()


def merge_findings_with_deduplication(existing_findings: list[dict[str, Any]], new_findings: list[dict[str, Any]], findings_type: FindingsType) -> list[dict[str, Any]]:
    """Merge new findings with existing ones, removing duplicates.

    Deduplication is based on source URL hash.

    Args:
        existing_findings: List of existing findings
        new_findings: List of new findings to merge
        findings_type: Type of the new findings

    Returns:
        Merged list of findings without duplicates
    """
    # Build set of existing hashes
    existing_hashes = {get_finding_hash(f) for f in existing_findings}

    # Add type to new findings and filter duplicates
    merged = list(existing_findings)

    for finding in new_findings:
        finding_hash = get_finding_hash(finding)
        if finding_hash not in existing_hashes:
            finding["findings_type"] = findings_type.value
            merged.append(finding)
            existing_hashes.add(finding_hash)

    return merged


def format_findings_for_display(findings: list[dict[str, Any]], max_text_length: int = 500) -> str:
    """Format findings list for display.

    Args:
        findings: List of findings to format
        max_text_length: Maximum text length per finding

    Returns:
        Formatted string representation
    """
    if not findings:
        return "No findings available."

    output = []
    for i, finding in enumerate(findings, 1):
        title = finding.get("title", "Untitled")
        source = finding.get("source", finding.get("url", "Unknown source"))
        text = finding.get("text", "")

        # Truncate text if needed
        if len(text) > max_text_length:
            text = text[:max_text_length] + "..."

        output.append(f"\n--- Finding {i}: {title} ---")
        output.append(f"Source: {source}")
        if text:
            output.append(f"Content: {text}")
        output.append("")

    return "\n".join(output)


def format_search_results(results: list[dict[str, Any]], source_type: str = "search") -> str:
    """Format search results for LLM consumption.

    Args:
        results: List of result dictionaries
        source_type: Type of search for labeling

    Returns:
        Formatted string for LLM context
    """
    if not results:
        return f"No {source_type} results found."

    formatted = f"{source_type.title()} Results:\n\n"

    for i, result in enumerate(results, 1):
        title = result.get("title", "Untitled")
        url = result.get("url", result.get("source", ""))
        text = result.get("text", result.get("content", ""))

        formatted += f"--- SOURCE {i}: {title} ---\n"
        if url:
            formatted += f"URL: {url}\n"
        if text:
            formatted += f"Content:\n{text}\n"
        formatted += "\n" + "-" * 40 + "\n\n"

    return formatted


def generate_source_id(base_name: str, source_registry: dict | None = None, researcher_idx: str | None = None, max_words: int = 4) -> str:
    """Generate a standardized source ID with only alphanumeric and underscore characters.

    Args:
        base_name: The base string to generate the ID from (e.g., title or keyword).
        source_registry: Optional dictionary of existing sources to avoid collisions.
        researcher_idx: Optional researcher index to prefix.
        max_words: Maximum number of words (separated by underscore) to include.

    Returns:
        A clean, alphanumeric-and-underscore snake_case identifier.
    """
    clean_name = str(base_name).lower()
    clean_name = re.sub(r"[^a-z0-9]", " ", clean_name)

    stop_words = {"a", "an", "the", "on", "in", "to", "of", "for", "with", "and", "or", "but"}
    words = [w for w in clean_name.split() if w and w not in stop_words]

    base_words = words[:max_words] if words else ["source"]
    base_id = "_".join(base_words)
    src_id = f"r{researcher_idx}_{base_id}" if researcher_idx is not None else base_id

    if source_registry is not None:
        original_src_id = src_id
        counter = 2
        while src_id in source_registry:
            src_id = f"{original_src_id}_{counter}"
            counter += 1

    return src_id
