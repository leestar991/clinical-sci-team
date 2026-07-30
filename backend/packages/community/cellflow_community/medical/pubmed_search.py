"""PubMed search tool for medical literature research.

This module provides a LangChain-compatible tool for searching PubMed,
the free database of biomedical and life sciences literature.

No API key required - uses the public NCBI E-utilities API.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx
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

# PubMed E-utilities API endpoints
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class PubMedClient:
    """Client for interacting with PubMed E-utilities API."""

    def __init__(self, tool_name: str = "open-deep-research", email: str = "research@example.com"):
        """Initialize PubMed client.

        Args:
            tool_name: Tool identifier for NCBI
            email: Contact email for NCBI (required by their policy)
        """
        self.tool_name = tool_name
        self.email = email
        # No long-lived httpx client: it would bind to the event loop that created
        # this (module-singleton) instance and fail when a subagent calls it from
        # another loop. The client is created per-request in _make_request instead.
        # The rate limiter IS shared (loop-neutral) so the NCBI cap stays global.

        # NCBI allows up to 3 req/sec without API key. Lower to 2.5 per user request to avoid 429s
        self.rate_limiter = AsyncDelayRateLimiter(requests_per_second=2.5)

    async def _make_request(self, url: str, params: dict, output_format: str = "json") -> Any:
        """Make an async, rate-limited request to NCBI API.

        Args:
            url: API endpoint URL
            params: Query parameters
            output_format: Response format (json or xml)

        Returns:
            Parsed response (dict for json, string for xml)
        """
        params.update(
            {
                "tool": self.tool_name,
                "email": self.email,
                "retmode": output_format,
            }
        )

        max_retries = 3
        # Per-call client (context-managed): created on the current running loop and
        # closed on every exit path; one client covers this call's retry attempts.
        async with httpx.AsyncClient() as client:
            for attempt in range(max_retries):
                # We acquire the global lock *per attempt* so retries are properly spaced out
                await self.rate_limiter.acquire()

                try:
                    response = await client.get(url, params=params, timeout=30.0)

                    if response.status_code == 429:
                        raise httpx.HTTPStatusError(f"429 Too Many Requests (attempt {attempt + 1})", request=response.request, response=response)

                    response.raise_for_status()

                    if output_format == "json":
                        return response.json()
                    return response.text

                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed to fetch from PubMed after {max_retries} attempts: {e}")
                        raise
                    # Exponential backoff before queuing back into the rate limiter
                    await asyncio.sleep(2**attempt + 1)

    async def search_ids(self, query: str, max_results: int = 10) -> list[str]:
        """Search PubMed and return article IDs.

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of PubMed article IDs
        """
        params = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
        }

        try:
            result = await self._make_request(ESEARCH_URL, params, "json")
            return result.get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            logger.error(f"PubMed search failed: {e}")
            raise

    async def fetch_articles(self, article_ids: list[str]) -> list[dict]:
        """Fetch article details by IDs.

        Args:
            article_ids: List of PubMed article IDs

        Returns:
            List of article dictionaries with title, abstract, authors, etc.
        """
        if not article_ids:
            return []

        params = {
            "db": "pubmed",
            "id": ",".join(article_ids),
            "rettype": "abstract",
        }

        try:
            xml_response = await self._make_request(EFETCH_URL, params, "xml")
            return self._parse_articles_xml(xml_response)
        except Exception as e:
            logger.error(f"PubMed fetch failed: {e}")
            raise

    def _parse_articles_xml(self, xml_string: str) -> list[dict]:
        """Parse PubMed XML response into article dictionaries.

        Args:
            xml_string: Raw XML response from efetch

        Returns:
            List of parsed article dictionaries
        """
        articles = []

        try:
            root = ET.fromstring(xml_string)

            for article_elem in root.findall(".//PubmedArticle"):
                article = self._parse_single_article(article_elem)
                if article:
                    articles.append(article)

        except ET.ParseError as e:
            logger.error(f"Failed to parse PubMed XML: {e}")

        return articles

    def _parse_single_article(self, article_elem: ET.Element) -> dict | None:
        """Parse a single PubMed article element.

        Args:
            article_elem: XML element for a PubmedArticle

        Returns:
            Parsed article dictionary or None
        """
        try:
            # Extract PubMed ID
            pmid_elem = article_elem.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else ""

            # Extract title
            title_elem = article_elem.find(".//ArticleTitle")
            title = title_elem.text if title_elem is not None else ""

            # Extract abstract
            abstract_parts = []
            for abstract_text in article_elem.findall(".//AbstractText"):
                label = abstract_text.get("Label", "")
                text = abstract_text.text or ""
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            # Extract authors
            authors = []
            for author in article_elem.findall(".//Author"):
                last_name = author.find("LastName")
                fore_name = author.find("ForeName")
                if last_name is not None and fore_name is not None:
                    authors.append(f"{fore_name.text} {last_name.text}")
                elif last_name is not None:
                    authors.append(last_name.text)

            # Extract journal
            journal_elem = article_elem.find(".//Journal/Title")
            journal = journal_elem.text if journal_elem is not None else ""

            # Extract publication date
            pub_date = self._extract_pub_date(article_elem)

            # Extract DOI
            doi = ""
            for article_id in article_elem.findall(".//ArticleId"):
                if article_id.get("IdType") == "doi":
                    doi = article_id.text or ""
                    break

            return {
                "pubmed_id": pmid,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "journal": journal,
                "publication_date": pub_date,
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            }

        except Exception as e:
            logger.error(f"Failed to parse article: {e}")
            return None

    def _extract_pub_date(self, article_elem: ET.Element) -> str:
        """Extract publication date from article element.

        Args:
            article_elem: XML element for a PubmedArticle

        Returns:
            Formatted date string (YYYY-MM-DD or partial)
        """
        pub_date_elem = article_elem.find(".//PubDate")
        if pub_date_elem is None:
            return ""

        year = pub_date_elem.find("Year")
        month = pub_date_elem.find("Month")
        day = pub_date_elem.find("Day")

        parts = []
        if year is not None:
            parts.append(year.text)
        if month is not None:
            parts.append(month.text)
        if day is not None:
            parts.append(day.text)

        return "-".join(parts) if parts else ""


# Global client instance
_pubmed_client: PubMedClient | None = None


def get_pubmed_client() -> PubMedClient:
    """Get or create global PubMed client instance."""
    global _pubmed_client
    if _pubmed_client is None:
        _pubmed_client = PubMedClient()
    return _pubmed_client


PUBMED_SEARCH_DESCRIPTION = (
    "Search PubMed (NCBI) for peer-reviewed biomedical literature — mechanism, efficacy/safety "
    "studies, meta-analyses, and reviews. USE for any 'studies show…' claim about biology, drugs, "
    "or disease. NOT for trial enrollment/results (use clinical_trials_search) or FDA approval "
    "status (use openfda_search). "
    "Pass `queries` as a LIST of focused queries (PubMed syntax OK, e.g. "
    "'amyloid[Title] AND 2023:2025[dp]'); several short queries with MeSH terms / synonyms beat one "
    "long phrase. `max_results` is per query. "
    "Returns per hit: title, abstract, authors, journal, year, and a "
    "https://pubmed.ncbi.nlm.nih.gov/{PMID}/ URL — cite that PMID URL and never invent a PMID. "
    "If a query returns nothing, retry with broader terms / a MeSH term before concluding the "
    "literature is silent. No API key required."
)


@tool(description=PUBMED_SEARCH_DESCRIPTION, response_format="content_and_artifact")
async def pubmed_search(topic: str, queries: list[str], max_results: int = 5, runtime: Runtime = None, config: RunnableConfig = None) -> tuple:
    """Search PubMed for medical literature.

    Args:
        queries: List of search queries for PubMed (supports PubMed query syntax)
        max_results: Maximum number of results to return per query (default: 5)
        topic: A simple sentence explaining why you are calling this tool and what you want to find. CRITICAL: This MUST be in the SAME language as the user's original query.
        config: Runtime configuration

    Returns:
        Tuple of (formatted_text, artifact_dict) where artifact contains source_registry
    """
    # Run synchronous PubMed API calls in thread pool
    client = get_pubmed_client()

    all_articles = []
    seen_ids = set()
    task_errors: list[str] = []

    # Execute queries concurrently up to the rate limit
    # The AsyncTokenBucketRateLimiter ensures we never exceed 3 req/sec overall
    search_tasks = [client.search_ids(query, max_results) for query in queries]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    for query, article_ids in zip(queries, search_results):
        if isinstance(article_ids, Exception):
            logger.error(f"PubMed search sub-task failed for query '{query}': {article_ids}")
            task_errors.append(f"query '{query}': {article_ids}")
            continue

        # Filter duplicates across queries
        new_ids = [aid for aid in article_ids if aid not in seen_ids]
        if not new_ids:
            continue

        try:
            articles = await client.fetch_articles(new_ids)
        except Exception as e:
            logger.error(f"PubMed fetch sub-task failed for query '{query}': {e}")
            task_errors.append(f"fetch for query '{query}': {e}")
            continue

        for art in articles:
            if art["pubmed_id"] not in seen_ids:
                all_articles.append(art)
                seen_ids.add(art["pubmed_id"])

    if not all_articles:
        if task_errors:
            return (f"No PubMed results were returned because the PubMed request failed: {'; '.join(task_errors[:3])}", {"source_registry": {}})
        return (f"No PubMed results found for queries: {queries}", {"source_registry": {}})

    # Build source registry and formatted output
    # Get researcher_idx from config metadata for unique prefixing
    researcher_idx = None
    if config and isinstance(config, dict):
        researcher_idx = config.get("metadata", {}).get("researcher_idx")

    source_registry = {}
    findings = []

    for article in all_articles:
        # Combine title and abstract for text content
        text_content = f"Title: {article['title']}\n\n"
        if article["abstract"]:
            text_content += f"Abstract: {article['abstract']}\n\n"
        if article["authors"]:
            text_content += f"Authors: {', '.join(article['authors'][:5])}"
            if len(article["authors"]) > 5:
                text_content += " et al."
            text_content += "\n"
        if article["journal"]:
            text_content += f"Journal: {article['journal']}\n"
        if article["publication_date"]:
            text_content += f"Published: {article['publication_date']}\n"

        finding = create_finding(
            title=article["title"],
            source=article["url"],
            text=text_content,
            url=article["url"],
            findings_type=FindingsType.PUBMED,
            authors=article["authors"],
            journal=article["journal"],
            publication_date=article["publication_date"],
            pubmed_id=article["pubmed_id"],
            doi=article.get("doi", ""),
        )
        findings.append(finding)

        # Create src_id
        src_id = generate_source_id(base_name=article["title"], source_registry=source_registry, researcher_idx=str(researcher_idx) if researcher_idx is not None else None, max_words=4)

        source_registry[src_id] = {
            "url": article["url"],
            "title": article["title"],
            "summary": text_content,
            "pmid": article.get("pmid"),
        }

    # Format results for LLM
    formatted_output = format_search_results(findings, "PubMed")

    # Record each article to the citation ledger and append a [cite:N] source block
    # (replaces the legacy snake-case source_registry JSON trailer, which was dead data).
    formatted_output += await _ledger.arecord_registry(runtime, source_registry, tool_name="pubmed_search", action="open")

    return (formatted_output, {"source_registry": source_registry})
