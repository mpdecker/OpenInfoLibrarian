"""Sci-Hub direct DOI search and metadata harvester."""

from __future__ import annotations

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

# Common Sci-Hub mirrors (these change frequently)
_DEFAULT_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.wf",
    "https://sci-hub.ren",
]


@register("scihub")
class ScihubSearcher(Searcher):
    """Search Sci-Hub by DOI to verify PDF availability.

    This searcher queries Sci-Hub to check if a DOI is available
    and extracts metadata from the response. Returns SearchHit
    with pdf_url pointing to the Sci-Hub PDF if found.
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        """Search Sci-Hub by DOI.

        Only supports DOI-based queries. For general text searches,
        use Crossref first to find DOIs, then query Sci-Hub.
        """
        doi = normalize_doi(query)
        if not doi:
            if kind == "doi":
                doi = query.strip()
            else:
                log.debug("scihub search requires DOI")
                return []

        mirrors = self.options.get("mirrors", _DEFAULT_MIRRORS)

        for mirror in mirrors:
            try:
                hit = await self._try_mirror(mirror, doi)
                if hit:
                    return [hit]
            except Exception as e:
                log.debug("scihub mirror %s failed for %s: %s", mirror, doi, e)
                continue

        return []

    async def _try_mirror(self, mirror: str, doi: str) -> SearchHit | None:
        """Try to resolve DOI on a specific Sci-Hub mirror."""
        # Sci-Hub URLs typically use /doi or direct DOI in path
        url = f"{mirror}/{doi}"

        html: str | None = None
        try:
            html = await self.fetcher.get_text(url, follow_redirects=True)
        except Exception as e:
            log.debug("scihub fetch failed: %s", e)

        if html is None:
            try:
                html = await self.fetcher.render(url)
            except Exception as e:
                log.debug("scihub render failed: %s", e)
                return None

        # Extract PDF URL from the HTML
        pdf_url = self._extract_pdf_url(html, mirror)
        if not pdf_url:
            return None

        # Try to extract metadata from the page
        title, authors, year = self._extract_metadata(html)

        return SearchHit(
            source="scihub",
            title=title or f"Sci-Hub: {doi}",
            authors=authors if authors else [],
            year=year,
            doi=doi,
            isbn=None,
            abstract="",
            url=url,
            pdf_url=pdf_url,
            score=1.0 if pdf_url else 0.0,
            extra={"mirror": mirror},
        )

    def _extract_pdf_url(self, html: str, mirror: str) -> str | None:
        """Extract PDF URL from Sci-Hub HTML."""
        import re

        # Common patterns for PDF URLs in Sci-Hub pages
        patterns = [
            # Direct iframe src
            r'<iframe[^>]*?src=["\']([^"\']*\.pdf[^"\']*)["\']',
            r'<iframe[^>]*?src=["\']([^"\']*/download[^"\']*)["\']',
            # Button/link hrefs
            r'<button[^>]*?onclick=["\'][^"\']*location\.href=["\']([^"\']*\.pdf[^"\']*)["\']',
            r'<a[^>]*?href=["\']([^"\']*\.pdf[^"\']*)["\'][^>]*?>[^<]*?(?:download|pdf)',
            # embed tag
            r'<embed[^>]*?src=["\']([^"\']*\.pdf[^"\']*)["\']',
            # location.href in script
            r'location\.href\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']',
            # PDF button pattern
            r'<button[^>]*?onclick=["\'][^"\']*location\.href=["\']([^"\']*)["\']',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                pdf_url = match.group(1)
                # Make absolute URL
                if pdf_url.startswith("//"):
                    pdf_url = "https:" + pdf_url
                elif pdf_url.startswith("/"):
                    pdf_url = mirror.rstrip("/") + pdf_url
                elif not pdf_url.startswith("http"):
                    pdf_url = mirror.rstrip("/") + "/" + pdf_url
                return pdf_url

        return None

    def _extract_metadata(self, html: str) -> tuple[str | None, list[str] | None, int | None]:
        """Extract title, authors, year from Sci-Hub HTML if available."""
        import re
        from html import unescape

        title = None
        authors = None
        year = None

        # Try to find title in various patterns
        title_patterns = [
            r'<title>(.*?)</title>',
            r'<h1[^>]*?>(.*?)</h1>',
            r'class=["\']title["\'][^>]*?>(.*?)</',
        ]

        for pattern in title_patterns:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                title = unescape(re.sub(r'<[^>]+>', '', match.group(1))).strip()
                # Remove Sci-Hub suffix if present
                title = re.sub(r'\s*[-–]\s*Sci-Hub.*$', '', title, flags=re.IGNORECASE)
                if title and len(title) > 3:
                    break

        # Try to extract authors - look for author patterns
        author_match = re.search(r'author[s]?[:\s]+([^<\n]+)', html, re.IGNORECASE)
        if author_match:
            author_text = author_match.group(1).strip()
            authors = [a.strip() for a in re.split(r'[,;]', author_text) if a.strip()]

        # Try to extract year
        year_match = re.search(r'(\d{4})', html)
        if year_match:
            y = int(year_match.group(1))
            if 1900 < y < 2100:
                year = y

        return title, authors, year


async def search_doi_on_scihub(fetcher, doi: str, mirrors: list[str] | None = None) -> str | None:
    """Utility function to directly resolve a DOI on Sci-Hub and return PDF URL.

    Args:
        fetcher: Fetcher instance for HTTP requests
        doi: DOI to search
        mirrors: Optional list of Sci-Hub mirrors to try

    Returns:
        PDF URL if found, None otherwise
    """
    mirrors = mirrors or _DEFAULT_MIRRORS
    normalized_doi = normalize_doi(doi) or doi

    searcher = ScihubSearcher(fetcher, mirrors=mirrors)
    hits = await searcher.search(normalized_doi, limit=1, kind="doi")

    if hits and hits[0].pdf_url:
        return hits[0].pdf_url
    return None
