"""Z-Library source.

Z-Library shuffles its public clear-net domains and hides behind
Cloudflare, but ``z-lib.fm`` and a small rotating set of mirrors still
serve plain server-side rendered HTML built around custom
``<z-bookcard>`` elements with ``href`` (catalog) and ``download``
(``/dl/<id>``) attributes.  We try those mirrors with plain HTTP first
and fall back to Playwright if the user has the ``[browser]`` extra
installed.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from documentcrawler.fetcher import FetchError
from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MIRRORS = [
    "https://z-lib.fm",
    "https://z-library.sk",
    "https://1lib.sk",
    "https://z-lib.io",
    "https://z-lib.gs",
]

_PER_MIRROR_TIMEOUT_S = 6.0
_CONNECT_TIMEOUT_S = 3.0


@register("zlibrary")
class ZLibrarySource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        mirrors = self._resolve_mirrors()

        title = ctx.metadata.title or ctx.query.title
        isbn = ctx.query.isbn or ctx.metadata.isbn
        query = isbn or title
        if not query:
            return []

        # The Searcher already exposes a direct ``/dl/...`` URL on its
        # ``SearchHit.pdf_url`` and the queue routes that into
        # ``DocumentQuery.url``.  When that's available, use it directly.
        seeded = ctx.query.url
        if seeded and "/dl/" in seeded and any(seeded.startswith(m) for m in mirrors):
            return [
                Candidate(
                    source=self.name,
                    url=seeded,
                    confidence=0.6,
                    note="zlibrary (seeded)",
                    needs_browser=True,
                )
            ]

        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/s/{quote(query)}"
            html = await self._fetch_mirror(ctx, url)
            if not html:
                continue

            cand = self._first_candidate_from_search(html, mirror)
            if cand is not None:
                return [cand]

        return []

    async def _fetch_mirror(self, ctx: SourceContext, url: str) -> str | None:
        try:
            return await asyncio.wait_for(
                ctx.fetcher.probe_text(
                    url,
                    timeout_s=_PER_MIRROR_TIMEOUT_S,
                    connect_s=_CONNECT_TIMEOUT_S,
                ),
                timeout=_PER_MIRROR_TIMEOUT_S,
            )
        except (TimeoutError, FetchError) as e:
            log.debug("zlibrary plain HTTP %s failed: %s — trying browser", url, e)
        except Exception as e:  # noqa: BLE001
            log.debug("zlibrary plain HTTP %s errored: %s", url, e)

        try:
            return await ctx.fetcher.render(url)
        except FetchError as e:
            log.debug("zlibrary browser render unavailable: %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("zlibrary render %s errored: %s", url, e)
        return None

    def _first_candidate_from_search(self, html: str, mirror: str) -> Candidate | None:
        """Return a downloadable candidate from a Z-Library search page.

        We pick the first ``<z-bookcard>`` that exposes a ``download``
        path; falling back to the catalog ``href`` (the Pipeline can
        then re-resolve the download link from the book page).
        """
        cleaned = html.replace("<!--", "").replace("-->", "")
        tree = HTMLParser(cleaned)

        for card in tree.css("z-bookcard"):
            download = (card.attributes.get("download") or "").strip()
            href = (card.attributes.get("href") or "").strip()
            if download:
                return Candidate(
                    source=self.name,
                    url=urljoin(mirror + "/", download),
                    confidence=0.55,
                    note="zlibrary",
                    needs_browser=True,
                )
            if href:
                return Candidate(
                    source=self.name,
                    url=urljoin(mirror + "/", href),
                    confidence=0.45,
                    note="zlibrary (book page)",
                    needs_browser=True,
                )

        # Legacy fallback for older mirrors that still use plain anchors.
        link = tree.css_first('a[href*="/book/"]')
        if link and link.attributes.get("href"):
            return Candidate(
                source=self.name,
                url=urljoin(mirror + "/", link.attributes["href"]),
                confidence=0.4,
                note="zlibrary (legacy)",
                needs_browser=True,
            )
        return None

    def _resolve_mirrors(self) -> list[str]:
        configured = self.options.get("mirrors") or []
        configured = [str(m).rstrip("/") for m in configured if m]
        if not configured:
            return list(_DEFAULT_MIRRORS)
        seen: set[str] = set()
        ordered: list[str] = []
        for m in configured + _DEFAULT_MIRRORS:
            if m in seen:
                continue
            seen.add(m)
            ordered.append(m)
        return ordered
