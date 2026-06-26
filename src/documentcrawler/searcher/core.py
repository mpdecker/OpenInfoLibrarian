"""CORE (core.ac.uk) metadata searcher - open access aggregator.

CORE aggregates millions of open access research papers from repositories
and journals worldwide. It provides both metadata and direct PDF links.
API key recommended for higher rate limits.
"""

from __future__ import annotations

from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_API_BASE = "https://api.core.ac.uk/v3"
_DEFAULT_TIMEOUT = 30.0


@register("core")
class CoreSearcher(Searcher):
    """Search CORE aggregate for open access papers.

    CORE provides metadata and often direct PDF links for OA content.
    Free tier: 10 requests/minute. API key increases limits.
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        api_key = self.options.get("api_key")

        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Build search request
        endpoint = f"{_API_BASE}/search/works"

        if kind == "doi" or (kind == "auto" and normalize_doi(query)):
            doi = normalize_doi(query) or query
            params: dict[str, Any] = {
                "q": f"doi:\"{doi}\"",
                "limit": min(limit, 100),
                "scroll": "false",
            }
        elif kind == "title":
            params = {
                "q": f"title:\"{query}\"",
                "limit": min(limit, 100),
                "scroll": "false",
            }
        elif kind == "author":
            params = {
                "q": f"authors:\"{query}\"",
                "limit": min(limit, 100),
                "scroll": "false",
            }
        else:
            params = {
                "q": query,
                "limit": min(limit, 100),
                "scroll": "false",
            }

        try:
            data = await self.fetcher.get_json(
                endpoint,
                params=params,
                headers=headers,
            )
        except Exception as e:
            log.debug("core search failed: %s", e)
            return []

        if not isinstance(data, dict):
            return []

        results = data.get("results") or data.get("works") or []
        if not isinstance(results, list):
            return []

        hits: list[SearchHit] = []
        for i, item in enumerate(results[:limit]):
            hit = self._item_to_hit(item, i)
            if hit:
                hits.append(hit)

        return hits

    def _item_to_hit(self, item: dict, index: int) -> SearchHit | None:
        """Convert a CORE work item to SearchHit."""
        if not isinstance(item, dict):
            return None

        title = item.get("title") or item.get("displayTitle")
        if not title:
            return None

        # Extract authors
        authors: list[str] = []
        author_data = item.get("authors") or item.get("author") or []
        if isinstance(author_data, list):
            for a in author_data:
                if isinstance(a, dict):
                    name = a.get("name") or f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                    if name:
                        authors.append(name)
                elif isinstance(a, str):
                    authors.append(a)

        # Year
        year = None
        pub_date = item.get("publishedDate") or item.get("yearPublished") or item.get("datePublished")
        if pub_date:
            import re
            m = re.search(r"(\d{4})", str(pub_date))
            if m:
                year = int(m.group(1))

        # DOI
        doi = None
        doi_data = item.get("doi") or item.get("identifiers", {}).get("doi")
        if doi_data:
            doi = normalize_doi(doi_data)

        # PDF URL - CORE often has direct download links
        pdf_url = None
        links = item.get("links") or item.get("downloadUrl") or []
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict):
                    url = link.get("url") or link.get("downloadUrl")
                    mime = link.get("mimeType", "").lower()
                    if url and ("pdf" in mime or url.lower().endswith(".pdf")):
                        pdf_url = url
                        break
                elif isinstance(link, str) and link.lower().endswith(".pdf"):
                    pdf_url = link
                    break
        elif isinstance(links, str):
            pdf_url = links if links.lower().endswith(".pdf") else None

        # Landing page URL
        url = item.get("links", [{}])[0].get("url") if isinstance(item.get("links"), list) else None
        if not url:
            url = f"https://core.ac.uk/works/{item.get('id')}"

        # Venue/publisher
        venue = None
        pub = item.get("publisher") or item.get("journals", [{}])[0].get("title")
        if pub:
            venue = pub

        score = max(0.05, 0.95 - index * 0.03)

        return SearchHit(
            source="core",
            title=str(title)[:500],
            authors=authors,
            year=year,
            doi=doi,
            isbn=item.get("identifiers", {}).get("isbn"),
            abstract=item.get("abstract") or item.get("description"),
            url=url,
            pdf_url=pdf_url,
            score=score,
            extra={
                "core_id": item.get("id"),
                "language": item.get("language", {}).get("code") if isinstance(item.get("language"), dict) else item.get("language"),
                "venue": venue,
                "full_text": item.get("fullText") is not None,
            },
        )
