"""LibGen source: scimag (papers) by DOI, libgen.is for books by ISBN/title."""

from __future__ import annotations

import contextlib
import re
from urllib.parse import quote, urljoin, urlparse

from selectolax.parser import HTMLParser

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MIRRORS = ["https://libgen.li", "https://libgen.gs", "https://libgen.is", "https://libgen.rs"]

_FILE_EXT_RE = re.compile(
    r'href="(/file\.php\?id=\d+)"[^<]*</a></nobr></td>\s*<td>([a-z0-9]+)</td>'
)
_ADS_MD5_RE = re.compile(r'href="/?(ads\.php\?md5=[0-9a-f]{32})"')


@register("libgen")
class LibgenSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        mirrors: list[str] = self.options.get("mirrors") or _DEFAULT_MIRRORS

        # A fuzzy resolver often attaches *some* DOI to a title query — a
        # book must not be routed to the papers path (and lose the book
        # search) just because of that guess. Try both sides: books when a
        # title/ISBN exists, scimag when a DOI exists; DOI-from-query
        # first, otherwise books first.
        book_candidates: list[Candidate] = []
        if ctx.metadata.isbn or ctx.query.isbn or ctx.metadata.title or ctx.query.title:
            book_candidates = await self._libgen_books(ctx, mirrors)

        doi = ctx.metadata.doi or ctx.query.doi
        scimag_candidates: list[Candidate] = []
        if doi:
            scimag_candidates = await self._scimag(ctx, mirrors, doi)

        if ctx.query.doi:
            return scimag_candidates + book_candidates
        return book_candidates + scimag_candidates

    async def _scimag(self, ctx: SourceContext, mirrors: list[str], doi: str) -> list[Candidate]:
        render_target: str | None = None
        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/scimag/?q={quote(doi)}"
            try:
                html = await ctx.fetcher.probe_text(url)
            except Exception:
                if render_target is None:
                    render_target = url
                continue

            mirror_links = _scimag_mirror_links(html, mirror)
            if mirror_links:
                return [
                    Candidate(
                        source=self.name,
                        url=link,
                        confidence=0.7,
                        note="libgen-scimag",
                        needs_browser=False,
                    )
                    for link in mirror_links[:3]
                ]
        # All mirrors failed plain HTTP — one browser attempt, not one per mirror.
        if render_target:
            with contextlib.suppress(Exception):
                html = await ctx.fetcher.render(render_target)
                mirror_links = _scimag_mirror_links(html, render_target)
                if mirror_links:
                    return [
                        Candidate(
                            source=self.name,
                            url=link,
                            confidence=0.7,
                            note="libgen-scimag",
                            needs_browser=False,
                        )
                        for link in mirror_links[:3]
                    ]
        return []

    async def _libgen_books(self, ctx: SourceContext, mirrors: list[str]) -> list[Candidate]:
        title = ctx.metadata.title or ctx.query.title
        isbn = ctx.query.isbn or ctx.metadata.isbn
        query = isbn or title
        if not query:
            return []

        # Fast single-attempt probes per mirror (dead hosts must not eat the
        # document budget); one browser render fallback at the very end.
        last_error_url: str | None = None
        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/index.php?req={quote(query)}"
            try:
                html = await ctx.fetcher.probe_text(url)
            except Exception:
                last_error_url = last_error_url or url
                continue

            candidates = _book_candidates_from_html(html, mirror)
            if candidates:
                host = urlparse(mirror).hostname or "libgen.li"
                return [
                    Candidate(
                        source=self.name,
                        url=u,
                        confidence=0.65 if u.startswith(mirror) else 0.6,
                        note=f"libgen:{host}",
                        needs_browser=False,
                    )
                    for u in candidates[:3]
                ]

        if last_error_url:
            with contextlib.suppress(Exception):
                html = await ctx.fetcher.render(last_error_url)
                candidates = _book_candidates_from_html(html, urlparse(last_error_url)
                                                        .scheme + "://" + urlparse(last_error_url).netloc)
                if candidates:
                    return [
                        Candidate(
                            source=self.name,
                            url=u,
                            confidence=0.6,
                            note="libgen:render",
                            needs_browser=False,
                        )
                        for u in candidates[:3]
                    ]
        return []


def _scimag_mirror_links(html: str, base: str) -> list[str]:
    """Extract the per-row mirror links (sci-hub / libgen.li / library.lol)."""
    tree = HTMLParser(html)
    out: list[str] = []
    for a in tree.css('table.catalog a, .catalog a'):
        href = a.attributes.get("href")
        if not href:
            continue
        if any(host in href for host in ("library.lol", "libgen.li", "sci-hub", "ads.php")):
            out.append(urljoin(base + "/", href))
    return list(dict.fromkeys(out))


def _book_candidates_from_html(html: str, mirror: str) -> list[str]:
    """Download candidate URLs from a libgen.li results page, best first.

    Rows expose a direct `/file.php?id=` link with the file's extension in
    the next cell — prefer those whose extension is pdf (the pipeline
    verifies PDF magic bytes; an epub/mobi download would be rejected).
    The ads.php interstitials follow as a fallback; the legacy
    library.lol mirror is gone (bad cert / 404) and is no longer used.
    """
    base = mirror.rstrip("/") + "/"
    direct_pdf: list[str] = []
    for m in _FILE_EXT_RE.finditer(html):
        path, ext = m.group(1), m.group(2).lower()
        if ext == "pdf":
            u = urljoin(base, path)
            if u not in direct_pdf:
                direct_pdf.append(u)
    interstitials = [
        urljoin(base, "/" + m.group(1))
        for m in _ADS_MD5_RE.finditer(html)
    ]
    seen: set[str] = set()
    out: list[str] = []
    for u in direct_pdf[:2] + list(dict.fromkeys(interstitials))[:2]:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:3]


def _first_book_md5(html: str) -> str | None:
    tree = HTMLParser(html)
    for a in tree.css('a[href*="md5="]'):
        href = a.attributes.get("href", "")
        if "md5=" in href:
            md5 = href.split("md5=", 1)[1].split("&", 1)[0].strip()
            if len(md5) == 32:
                return md5
    return None
