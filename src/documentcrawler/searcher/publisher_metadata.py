"""Publisher metadata extractors - harvest identifiers from major publishers.

These searchers query publisher APIs and landing pages to extract metadata
without attempting PDF access. The identifiers returned (DOI, ISBN, title,
authors) can be used with shadow library sources for actual document retrieval.

Supported publishers:
- Elsevier (ScienceDirect) - requires API key
- Springer - basic metadata via API
- Wiley - limited public API
- IEEE Xplore - requires API key
- Oxford Academic - metadata extraction
"""

from __future__ import annotations

import contextlib
from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)


@register("elsevier")
class ElsevierSearcher(Searcher):
    """Elsevier ScienceDirect metadata searcher.

    Requires an API key from https://dev.elsevier.com/
    Returns metadata only - no PDF URLs (paywalled).
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        api_key = self.options.get("api_key")
        if not api_key:
            log.debug("Elsevier search skipped: no api_key configured")
            return []

        # Elsevier Search API
        base_url = "https://api.elsevier.com/content/search/sciencedirect"
        params: dict[str, Any] = {
            "query": query,
            "count": min(limit, 100),
            "apiKey": api_key,
        }

        try:
            data = await self.fetcher.get_json(base_url, params=params)
        except Exception as e:
            log.debug("Elsevier search failed: %s", e)
            return []

        entries = (((data or {}).get("search-results") or {}).get("entry")) or []
        hits: list[SearchHit] = []
        for i, entry in enumerate(entries):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_entry(entry, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_entry(self, entry: dict[str, Any], score: float) -> SearchHit | None:
        title = entry.get("dc:title")
        if not title:
            return None

        doi = normalize_doi(entry.get("prism:doi"))
        authors_raw = entry.get("authors") or entry.get("dc:creator", "")
        authors: list[str] = []
        if isinstance(authors_raw, str):
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
        elif isinstance(authors_raw, list):
            authors = [str(a).strip() for a in authors_raw if str(a).strip()]

        year: int | None = None
        cover_date = entry.get("prism:coverDate", "")
        if cover_date and len(cover_date) >= 4 and cover_date[:4].isdigit():
            year = int(cover_date[:4])

        container = entry.get("prism:publicationName") or entry.get("prism:aggregationType")
        publisher = "Elsevier"

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=f"https://doi.org/{doi}" if doi else None,
            score=score,
            extra={
                "publisher": publisher,
                "publication": container,
                "pii": entry.get("pii"),  # Publication Item Identifier
            },
        )


@register("springer")
class SpringerSearcher(Searcher):
    """Springer Nature metadata searcher.

    Uses Springer Metadata API (open access to metadata).
    Returns metadata only - no PDF URLs.
    """

    _API = "https://api.springernature.com/meta/v2/json"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        api_key = self.options.get("api_key")
        params: dict[str, Any] = {
            "q": query,
            "p": min(limit, 50),
            "s": 1,
        }
        if api_key:
            params["api_key"] = api_key

        try:
            data = await self.fetcher.get_json(self._API, params=params)
        except Exception as e:
            log.debug("Springer search failed: %s", e)
            return []

        records = ((data or {}).get("records")) or []
        hits: list[SearchHit] = []
        for i, rec in enumerate(records):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_record(rec, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_record(self, rec: dict[str, Any], score: float) -> SearchHit | None:
        title = rec.get("title")
        if not title:
            return None

        doi = normalize_doi(rec.get("doi"))
        authors: list[str] = []
        for creator in rec.get("creators", []):
            name = creator.get("creator")
            if name:
                authors.append(name)

        year: int | None = None
        pub_date = rec.get("publicationDate", "")
        if pub_date and len(pub_date) >= 4 and pub_date[:4].isdigit():
            year = int(pub_date[:4])

        container = rec.get("publicationName")
        publisher = rec.get("publisher")

        # Build URL from DOI or use Springer link
        url = f"https://doi.org/{doi}" if doi else rec.get("url")

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=url,
            abstract=rec.get("abstract"),
            score=score,
            extra={
                "publisher": publisher,
                "genre": rec.get("genre"),
                "openaccess": rec.get("openaccess"),
            },
        )


@register("ieee")
class IEEESearcher(Searcher):
    """IEEE Xplore metadata searcher.

    Requires IEEE Xplore API key from https://developer.ieee.org/
    Returns metadata only - PDFs are behind paywall/subscription.
    """

    _API = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        api_key = self.options.get("api_key")
        if not api_key:
            log.debug("IEEE search skipped: no api_key configured")
            return []

        params: dict[str, Any] = {
            "querytext": query,
            "max_records": min(limit, 200),
            "apikey": api_key,
            "format": "json",
        }

        try:
            data = await self.fetcher.get_json(self._API, params=params)
        except Exception as e:
            log.debug("IEEE search failed: %s", e)
            return []

        articles = ((data or {}).get("articles")) or []
        hits: list[SearchHit] = []
        for i, article in enumerate(articles):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_article(article, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_article(self, article: dict[str, Any], score: float) -> SearchHit | None:
        title = article.get("title")
        if not title:
            return None

        doi = normalize_doi(article.get("doi"))
        authors: list[str] = []
        for author in article.get("authors", {}).get("authors", []):
            full_name = author.get("full_name")
            if full_name:
                authors.append(full_name)

        year: int | None = None
        pub_year = article.get("publication_year")
        if pub_year:
            with contextlib.suppress(TypeError, ValueError):
                year = int(pub_year)

        container = article.get("publication_title")
        publisher = "IEEE"

        # IEEE URL
        article_number = article.get("article_number")
        url = f"https://ieeexplore.ieee.org/document/{article_number}" if article_number else None
        if doi and not url:
            url = f"https://doi.org/{doi}"

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=url,
            abstract=article.get("abstract"),
            score=score,
            extra={
                "publisher": publisher,
                "content_type": article.get("content_type"),
                "article_number": article_number,
            },
        )


@register("wiley")
class WileySearcher(Searcher):
    """Wiley Online Library metadata searcher.

    Uses Crossref as backend since Wiley's API requires partnership.
    Returns metadata harvested from Crossref for Wiley publications.
    """

    _CROSSREF_API = "https://api.crossref.org/works"

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        # Search Crossref for Wiley publications
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "filter": "publisher-name:Wiley",
            "rows": min(limit, 50),
        }

        mailto = self.options.get("mailto")
        if mailto:
            params["mailto"] = mailto

        try:
            data = await self.fetcher.get_json(self._CROSSREF_API, params=params)
        except Exception as e:
            log.debug("Wiley search failed: %s", e)
            return []

        items = (((data or {}).get("message") or {}).get("items")) or []
        hits: list[SearchHit] = []
        for i, item in enumerate(items):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_crossref(item, score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_crossref(self, msg: dict[str, Any], score: float) -> SearchHit | None:
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
            (msg.get("issued") or {}).get("date-parts") or [[None]]
        )
        if date_parts and date_parts[0] and date_parts[0][0]:
            with contextlib.suppress(TypeError, ValueError):
                year = int(date_parts[0][0])

        doi = msg.get("DOI")
        container = (msg.get("container-title") or [None])[0]

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=f"https://doi.org/{doi}" if doi else None,
            abstract=_strip_jats(msg.get("abstract")),
            score=score,
            extra={
                "publisher": "Wiley",
                "type": msg.get("type"),
            },
        )


def _strip_jats(s: str | None) -> str | None:
    """Strip JATS XML tags from abstract text."""
    if not s:
        return None
    import re
    return re.sub(r"<[^>]+>", "", s).strip() or None
