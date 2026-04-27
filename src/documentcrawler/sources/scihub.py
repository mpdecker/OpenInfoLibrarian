"""Sci-Hub source: DOI -> mirror -> embedded PDF link.

Disabled by default. Mirror list is configurable; on each run we ping mirrors
and use the first responsive one.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from documentcrawler.fetcher import FetchError
from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.ru",
    "https://sci-hub.st",
    "https://sci-hub.ee",
]


@register("scihub")
class SciHubSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        doi = ctx.metadata.doi or ctx.query.doi
        if not doi:
            return []

        mirrors: list[str] = self.options.get("mirrors") or _DEFAULT_MIRRORS

        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            try:
                html = await ctx.fetcher.get_text(f"{mirror}/{doi}")
            except FetchError as e:
                log.debug("sci-hub mirror %s failed: %s", mirror, e)
                continue
            except Exception as e:
                log.debug("sci-hub mirror %s error: %s", mirror, e)
                continue

            pdf = _extract_pdf_url(html, mirror)
            if pdf:
                return [
                    Candidate(
                        source=self.name,
                        url=pdf,
                        confidence=0.8,
                        note=f"scihub:{urlparse(mirror).hostname}",
                    )
                ]

        return []


def _extract_pdf_url(html: str, mirror: str) -> str | None:
    """Sci-Hub embeds the PDF in either an <iframe>, <embed>, or via a JS button."""
    tree = HTMLParser(html)

    for selector in ("iframe#pdf", "embed[type='application/pdf']", "iframe", "embed"):
        node = tree.css_first(selector)
        if node and node.attributes.get("src"):
            return _absolutize(node.attributes["src"], mirror)

    # Newer mirrors place the PDF URL in a button onclick="location.href='...'"
    button = tree.css_first("button[onclick]")
    if button:
        onclick = button.attributes.get("onclick", "") or ""
        if "location.href=" in onclick:
            url = onclick.split("location.href=", 1)[1].strip().strip("';\"")
            if url:
                return _absolutize(url, mirror)

    # Fallback: any anchor pointing at a .pdf
    a = tree.css_first('a[href$=".pdf"]')
    if a:
        href = a.attributes.get("href")
        if href:
            return _absolutize(href, mirror)

    return None


def _absolutize(url: str, base: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base.rstrip("/") + "/", url.lstrip("/"))
