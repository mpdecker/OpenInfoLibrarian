"""JSTOR metadata searcher - harvests identifiers from JSTOR catalog."""

from __future__ import annotations

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_JSTOR_SEARCH = "https://www.jstor.org/api/search/v2"
_JSTOR_ITEM = "https://www.jstor.org/stable/"


@register("jstor")
class JstorSearcher(Searcher):
    """Search JSTOR catalog for metadata and identifiers.

    JSTOR is primarily a paywalled archive but provides metadata
    including DOIs that can be used with shadow libraries.
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        try:
            # JSTOR's API is not officially public; fallback to page scraping pattern
            # Using their search endpoint if available, otherwise construct direct URLs
            hits = await self._search_via_page_scrape(query, limit, kind)
            return hits
        except Exception as e:
            log.debug("jstor search failed: %s", e)
            return []

    async def _search_via_page_scrape(self, query: str, limit: int, kind: str) -> list[SearchHit]:
        """Fallback: Search via JSTOR's public search page parsing."""
        import urllib.parse

        # Build search URL
        params = {"q": query, "acc": "on"}  # acc=on includes open access
        if kind == "title":
            params["Query"] = f"ti:{query}"
        elif kind == "author":
            params["Query"] = f"au:{query}"

        search_url = f"https://www.jstor.org/action/doBasicSearch?{urllib.parse.urlencode(params)}"

        try:
            html = await self.fetcher.get_text(search_url)
        except Exception as e:
            log.debug("jstor page fetch failed: %s", e)
            return []

        hits: list[SearchHit] = []

        # Parse results from JSTOR HTML
        # Look for data-result-item patterns or JSON-LD
        import re

        # Try to extract from JSON-LD script tags
        jsonld_pattern = r'<script type="application/ld\+json">(.*?)</script>'
        for match in re.finditer(jsonld_pattern, html, re.DOTALL):
            try:
                import json
                data = json.loads(match.group(1))
                if isinstance(data, dict) and data.get("@type") in ["ScholarlyArticle", "Article"]:
                    hit = self._hit_from_jsonld(data)
                    if hit:
                        hits.append(hit)
                        if len(hits) >= limit:
                            break
            except Exception:
                continue

        # Fallback: Parse result items from HTML structure
        if not hits:
            hits = self._parse_html_results(html, limit)

        return hits[:limit]

    def _hit_from_jsonld(self, data: dict) -> SearchHit | None:
        """Create SearchHit from JSON-LD structured data."""
        title = data.get("name") or data.get("headline", "")
        if not title:
            return None

        authors: list[str] = []
        author_data = data.get("author")
        if isinstance(author_data, list):
            for a in author_data:
                if isinstance(a, dict):
                    authors.append(a.get("name", ""))
                elif isinstance(a, str):
                    authors.append(a)
        elif isinstance(author_data, dict):
            authors.append(author_data.get("name", ""))
        elif isinstance(author_data, str):
            authors.append(author_data)

        doi = normalize_doi(data.get("doi", ""))
        url = data.get("url", "")

        # Extract year from date
        year = None
        date = data.get("datePublished", "")
        if date:
            import re
            m = re.search(r"(\d{4})", str(date))
            if m:
                year = int(m.group(1))

        # Venue
        venue = ""
        journal = data.get("isPartOf", {})
        if isinstance(journal, dict):
            venue = journal.get("name", "")

        return SearchHit(
            source="jstor",
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            isbn=None,
            abstract=data.get("description", ""),
            url=url,
            pdf_url=None,  # JSTOR is paywalled - no direct PDF
            score=0.8,
            extra={
                "container": venue,
                "volume": data.get("volumeNumber"),
                "issue": data.get("issueNumber"),
            },
        )

    def _parse_html_results(self, html: str, limit: int) -> list[SearchHit]:
        """Parse search results from JSTOR HTML."""
        import re
        from html import unescape

        hits: list[SearchHit] = []

        # Look for result items
        # Pattern: data-result-item or result-item class
        result_pattern = r'<(?:div|li)[^>]*?(?:data-result-item|class="[^"]*?result-item)[^>]*?>(.*?)</(?:div|li)>'

        for match in re.finditer(result_pattern, html, re.DOTALL | re.IGNORECASE):
            if len(hits) >= limit:
                break

            item_html = match.group(1)

            # Extract title
            title_match = re.search(r'<a[^>]*?class="[^"]*?title[^"]*?"[^>]*?>(.*?)</a>', item_html, re.DOTALL | re.IGNORECASE)
            if not title_match:
                title_match = re.search(r'<h[23][^>]*?>(.*?)</h[23]>', item_html, re.DOTALL | re.IGNORECASE)

            title = unescape(re.sub(r'<[^>]+>', '', title_match.group(1))) if title_match else ""
            if not title:
                continue

            # Extract authors
            authors: list[str] = []
            author_match = re.search(r'class="[^"]*?author[^"]*?"[^>]*?>(.*?)</span>', item_html, re.DOTALL | re.IGNORECASE)
            if author_match:
                author_text = unescape(re.sub(r'<[^>]+>', '', author_match.group(1)))
                authors = [a.strip() for a in author_text.split(",") if a.strip()]

            # Extract DOI
            doi = None
            doi_match = re.search(r'doi[:/](10\.\d{4,}/[^"\s<>]+)', item_html, re.IGNORECASE)
            if doi_match:
                doi = normalize_doi(doi_match.group(1))

            # Extract URL/stable ID
            url = ""
            url_match = re.search(r'href="(/stable/\d+)"', item_html)
            if url_match:
                url = f"https://www.jstor.org{url_match.group(1)}"

            # Extract year
            year = None
            year_match = re.search(r'(\d{4})', item_html)
            if year_match:
                y = int(year_match.group(1))
                if 1800 < y < 2100:
                    year = y

            hits.append(SearchHit(
                source="jstor",
                title=title,
                authors=authors,
                year=year,
                doi=doi,
                isbn=None,
                abstract="",
                url=url,
                pdf_url=None,
                score=0.6,
            ))

        return hits
