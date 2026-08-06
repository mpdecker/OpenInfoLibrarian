"""Anna's Archive source.

Search the public search endpoint by title / ISBN / MD5, follow the top result
to its detail page, and emit slow_download candidates.
"""

from __future__ import annotations

import contextlib
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)


@register("annas_archive")
class AnnasArchiveSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        base = (self.options.get("base_url") or "https://annas-archive.org").rstrip("/")
        mirrors: list[str] = self.options.get("mirrors") or [
            "https://annas-archive.org",
            "https://annas-archive.gl",
            "https://annas-archive.se",
            "https://annas-archive.li",
        ]
        if base.rstrip("/") not in {m.rstrip("/") for m in mirrors}:
            mirrors.insert(0, base)

        query = self._build_query(ctx)
        if not query:
            return []

        for base in mirrors:
            base = base.rstrip("/")
            search_url = f"{base}/search?q={quote(query)}"
            html: str | None = None
            try:
                html = await ctx.fetcher.get_text(search_url)
            except Exception as e:
                log.debug("annas search request failed: %s", e)

            if html is None:
                try:
                    html = await ctx.fetcher.render(search_url)
                except Exception as e2:
                    log.debug("annas search render failed: %s", e2)
                    continue

            md5 = _first_md5(html)
            if not md5:
                continue

            detail_url = f"{base}/md5/{md5}"
            detail_html: str | None = None
            with contextlib.suppress(Exception):
                detail_html = await ctx.fetcher.get_text(detail_url)

            if detail_html is None:
                try:
                    detail_html = await ctx.fetcher.render(detail_url)
                except Exception:
                    continue

            candidates: list[Candidate] = []
            for href in _slow_download_links(detail_html, base):
                candidates.append(
                    Candidate(
                        source=self.name,
                        url=href,
                        confidence=0.7,
                        note=f"annas:{md5}",
                        needs_browser=True,
                    )
                )
            for href in _ipfs_links(detail_html):
                candidates.append(
                    Candidate(
                        source=self.name,
                        url=href,
                        confidence=0.6,
                        note="annas:ipfs",
                    )
                )
            if candidates:
                return candidates

        return []

    def _build_query(self, ctx: SourceContext) -> str | None:
        if ctx.metadata.doi:
            return ctx.metadata.doi
        if ctx.query.isbn:
            return ctx.query.isbn
        title = ctx.metadata.title or ctx.query.title
        if not title:
            return None
        author = (ctx.metadata.authors or ctx.query.authors or [""])[0]
        return f"{title} {author}".strip()


def _first_md5(html: str) -> str | None:
    tree = HTMLParser(html)
    for a in tree.css('a[href*="/md5/"]'):
        href = a.attributes.get("href", "")
        if href and "/md5/" in href:
            md5 = href.rsplit("/md5/", 1)[-1].split("?", 1)[0].strip("/")
            if len(md5) == 32:
                return md5
    return None


def _slow_download_links(html: str, base: str) -> list[str]:
    tree = HTMLParser(html)
    out: list[str] = []
    for a in tree.css('a[href*="/slow_download/"]'):
        href = a.attributes.get("href")
        if href:
            out.append(urljoin(base + "/", href))
    return out


def _ipfs_links(html: str) -> list[str]:
    tree = HTMLParser(html)
    out: list[str] = []
    for a in tree.css('a[href*="ipfs"]'):
        href = a.attributes.get("href", "")
        if href.startswith("http") and "ipfs" in href:
            out.append(href)
    return out
