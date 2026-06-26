"""DOI.org metadata resolver - extracts identifiers from DOI registry.

This searcher queries doi.org and Crossref to harvest publication metadata
without attempting PDF access. The identifiers returned can be used with
shadow library sources for actual document retrieval.
"""

from __future__ import annotations

from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_RESOLVER_URL = "https://doi.org"
_CROSSREF_API = "https://api.crossref.org/works"


@register("doi_org")
class DoiOrgSearcher(Searcher):
    """Resolve DOIs via doi.org and Crossref to get publication metadata.

    Returns metadata including DOI, title, authors, publisher, year.
    No PDF URLs are returned - this searcher is for identifier harvesting only.
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        # If query looks like a DOI, resolve it directly
        doi = normalize_doi(query)
        if doi:
            return await self._resolve_doi(doi)

        # Otherwise, search Crossref for matching works
        return await self._search_crossref(query, limit)

    async def _resolve_doi(self, doi: str) -> list[SearchHit]:
        """Resolve a single DOI via Crossref API."""
        try:
            data = await self.fetcher.get_json(f"{_CROSSREF_API}/{doi}")
        except Exception as e:
            log.debug("doi.org resolution failed for %s: %s", doi, e)
            return []

        msg = data.get("message") if isinstance(data, dict) else None
        if not msg:
            return []

        hit = self._hit_from_crossref_msg(msg, score=1.0)
        return [hit] if hit else []

    async def _search_crossref(self, query: str, limit: int) -> list[SearchHit]:
        """Search Crossref for works matching query."""
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": min(limit, 50),
            "select": "DOI,title,author,issued,container-title,publisher,type,URL,abstract",
        }

        mailto = self.options.get("mailto")
        if mailto:
            params["mailto"] = mailto

        try:
            data = await self.fetcher.get_json(_CROSSREF_API, params=params)
        except Exception as e:
            log.debug("crossref search failed: %s", e)
            return []

        items = (((data or {}).get("message") or {}).get("items")) or []
        hits: list[SearchHit] = []
        for i, item in enumerate(items):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_crossref_msg(item, score=score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_crossref_msg(self, msg: dict[str, Any], *, score: float) -> SearchHit | None:
        """Build a SearchHit from Crossref message data."""
        if not msg:
            return None

        title = (msg.get("title") or [None])[0]
        if not title:
            return None

        authors: list[str] = []
        for a in msg.get("author") or []:
            given = a.get("given") or ""
            family = a.get("family") or ""
            full = (given + " " + family).strip() or a.get("name") or ""
            if full:
                authors.append(full)

        year: int | None = None
        date_parts = (
            (msg.get("issued") or msg.get("published-online") or msg.get("published-print") or {}).get("date-parts")
            or [[None]]
        )
        if date_parts and date_parts[0] and date_parts[0][0]:
            try:
                year = int(date_parts[0][0])
            except (TypeError, ValueError):
                year = None

        doi = msg.get("DOI")
        container = (msg.get("container-title") or [None])[0]
        publisher = msg.get("publisher")

        # Build landing page URL from DOI
        url = msg.get("URL") or (f"https://doi.org/{doi}" if doi else None)

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=url,
            abstract=_strip_jats(msg.get("abstract")),
            score=score,
            extra={
                "type": msg.get("type"),
                "publisher": publisher,
                "resolved_via": "crossref",
            },
        )


def _strip_jats(s: str | None) -> str | None:
    """Strip JATS XML tags from abstract text."""
    if not s:
        return None
    import re
    return re.sub(r"<[^>]+>", "", s).strip() or None
