"""LibGen source: scimag (papers) by DOI, libgen.is for books by ISBN/title."""

from __future__ import annotations

import contextlib
from urllib.parse import quote, urljoin, urlparse

from selectolax.parser import HTMLParser

from documentcrawler.fetcher import FetchError
from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MIRRORS = ["https://libgen.li", "https://libgen.gs", "https://libgen.is", "https://libgen.rs"]


@register("libgen")
class LibgenSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        mirrors: list[str] = self.options.get("mirrors") or _DEFAULT_MIRRORS

        if ctx.metadata.doi:
            return await self._scimag(ctx, mirrors, ctx.metadata.doi)
        return await self._libgen_books(ctx, mirrors)

    async def _scimag(self, ctx: SourceContext, mirrors: list[str], doi: str) -> list[Candidate]:
        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/scimag/?q={quote(doi)}"
            html: str | None = None
            try:
                html = await ctx.fetcher.get_text(url)
            except FetchError:
                pass
            except Exception:
                pass

            if html is None:
                try:
                    html = await ctx.fetcher.render(url)
                except Exception:
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
        return []

    async def _libgen_books(self, ctx: SourceContext, mirrors: list[str]) -> list[Candidate]:
        title = ctx.metadata.title or ctx.query.title
        isbn = ctx.query.isbn or ctx.metadata.isbn
        query = isbn or title
        if not query:
            return []

        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/index.php?req={quote(query)}"
            html: str | None = None
            with contextlib.suppress(Exception):
                html = await ctx.fetcher.get_text(url)

            if html is None:
                try:
                    html = await ctx.fetcher.render(url)
                except Exception:
                    continue

            md5 = _first_book_md5(html)
            if not md5:
                continue
            host = urlparse(mirror).hostname or "libgen.li"
            return [
                Candidate(
                    source=self.name,
                    url=f"https://library.lol/main/{md5}",
                    confidence=0.65,
                    note=f"libgen:{host}",
                    needs_browser=False,
                ),
                Candidate(
                    source=self.name,
                    url=f"https://libgen.li/ads.php?md5={md5}",
                    confidence=0.6,
                    note="libgen:ads",
                    needs_browser=False,
                ),
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


def _first_book_md5(html: str) -> str | None:
    tree = HTMLParser(html)
    for a in tree.css('a[href*="md5="]'):
        href = a.attributes.get("href", "")
        if "md5=" in href:
            md5 = href.split("md5=", 1)[1].split("&", 1)[0].strip()
            if len(md5) == 32:
                return md5
    return None
