"""Sci-Hub source: DOI -> mirror -> embedded PDF link.

Disabled by default. Mirror list is configurable; on each run we ping mirrors
and use the first responsive one.
"""

from __future__ import annotations

import re
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
    "https://sci-hub.wf",
    "https://sci-hub.ren",
]

# Mirrors frequently front a download CDN instead of serving the PDF
# themselves (iframe/embed URLs absolutize to these partner hosts).
_PARTNER_HOSTS = ("sci.bban.top",)

_ONCLICK_HREF_RE = re.compile(r"location\s*\.\s*href\s*=\s*['\"]([^'\"]+)['\"]")


@register("scihub")
class SciHubSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        doi = ctx.metadata.doi or ctx.query.doi
        if not doi:
            return []

        mirrors: list[str] = self.options.get("mirrors") or _DEFAULT_MIRRORS

        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            html: str | None = None
            try:
                html = await ctx.fetcher.get_text(f"{mirror}/{doi}")
            except FetchError as e:
                log.debug("sci-hub mirror %s http failed: %s", mirror, e)
            except Exception as e:
                log.debug("sci-hub mirror %s error: %s", mirror, e)

            if html is None:
                try:
                    html = await ctx.fetcher.render(f"{mirror}/{doi}")
                except Exception as e:
                    log.debug("sci-hub mirror %s render failed: %s", mirror, e)
                    continue

            if _is_verification_page(html):
                log.debug("sci-hub mirror %s served a verification page for %s", mirror, doi)
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


def _is_verification_page(html: str) -> bool:
    """Captcha / 'Verification' interstitials some mirrors serve instead
    of article pages — they contain no PDF and must not count as a miss
    for the whole source, just for this mirror."""
    tree = HTMLParser(html)
    title = (tree.css_first("title").text() if tree.css_first("title") else "") or ""
    lowered = title.lower()
    return "verification" in lowered or "captcha" in lowered


def _is_scihub_download(url: str, mirror: str) -> bool:
    """Guard against extracting links that are NOT Sci-Hub PDFs.

    Article pages legitimately link the *original publisher* (doi.org,
    academic.oup.com, ...) — following those from here walks into
    Cloudflare and logs a nonsense 'scihub' attempt. A real download URL
    lives on the mirror itself, a known partner CDN, or a /pdf/ or
    /downloads/ style path.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    mirror_host = (urlparse(mirror).hostname or "").lower()
    if not host:
        return False
    if host == mirror_host:
        return True
    if host in _PARTNER_HOSTS:
        return True
    if host.endswith(".sci-hub.se") or host in (
        "sci-hub.ru", "sci-hub.st", "sci-hub.ee", "sci-hub.wf", "sci-hub.ren",
    ):
        return True
    path = (parsed.path or "").lower()
    return path.startswith("/pdf/") or path.startswith("/downloads/")


def _extract_pdf_url(html: str, mirror: str) -> str | None:
    """Sci-Hub embeds the PDF in either an <iframe>, <embed>, or via a JS button."""
    tree = HTMLParser(html)

    # Ordered most-specific first; every extraction is guarded so an
    # original-publisher link is never returned.
    for selector in ("iframe#pdf", "embed[type='application/pdf']"):
        node = tree.css_first(selector)
        if node and node.attributes.get("src"):
            url = _absolutize(node.attributes["src"], mirror)
            if _is_scihub_download(url, mirror):
                return url

    # Newer mirrors place the PDF URL in a button onclick="location.href='...'"
    button = tree.css_first("button[onclick]")
    if button:
        onclick = button.attributes.get("onclick", "") or ""
        m = _ONCLICK_HREF_RE.search(onclick)
        if m:
            url = _absolutize(m.group(1), mirror)
            if _is_scihub_download(url, mirror):
                return url

    # Any anchor pointing at a .pdf …
    for a in tree.css('a[href$=".pdf"]'):
        href = a.attributes.get("href")
        if href:
            url = _absolutize(href, mirror)
            if _is_scihub_download(url, mirror):
                return url

    # … and finally a generic iframe/embed (ads and captchas live in
    # iframes too, hence last and guarded).
    for selector in ("iframe", "embed"):
        node = tree.css_first(selector)
        if node and node.attributes.get("src"):
            url = _absolutize(node.attributes["src"], mirror)
            if _is_scihub_download(url, mirror):
                return url

    return None


def _absolutize(url: str, base: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base.rstrip("/") + "/", url.lstrip("/"))
