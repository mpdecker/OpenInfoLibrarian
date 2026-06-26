"""Z-Library search (shadow library).

Z-Library hosts move frequently and most landing pages are heavily
Cloudflare-gated, but the actual search HTML on the *clear-net* mirrors
(``z-lib.fm`` is the most reliable as of 2026, with ``z-library.sk`` /
``1lib.sk`` / ``z-lib.gs`` rotating in and out) is plain server-side
rendered markup using a custom ``<z-bookcard>`` web component.

Each card looks like::

    <z-bookcard
        id="5206515"
        href="/book/n0z2rJ4q0k/machine-learning-…"
        download="/dl/0rLbxVm3MZ"
        publisher="…"
        year="2016"
        language="English"
        extension="pdf"
        filesize="2.39 MB">
      <div slot="title">Machine Learning Mastery with Python …</div>
      <div slot="author">Jason Brownlee</div>
      …
    </z-bookcard>

Mirrors that respond with a Cloudflare challenge / 503 are simply
skipped and we fall through to the next entry in the list.  When a user
has the ``[browser]`` extra installed the searcher will additionally
retry through the Playwright pool for the first mirror that returned a
challenge — that handles the "logged-in only" gating some hosts add.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

# Mirrors are tried in priority order; the working set rotates often
# enough that we keep a generous list and skip dead ones quickly.
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
class ZLibrarySearcher(Searcher):
    """Scrape Z-Library search results.

    Each hit's ``url`` is the catalog landing page; ``pdf_url`` is set to
    the ``/dl/...`` direct download path that Z-Library exposes when the
    user is anonymous (the link itself usually requires a session — we
    surface it so a Source can take over downloading).
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        if not query or not query.strip():
            return []

        mirrors = self._resolve_mirrors()

        last_err: Exception | None = None
        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            url = f"{mirror}/s/{quote(query)}"
            html: str | None = None
            try:
                html = await asyncio.wait_for(
                    self.fetcher.probe_text(
                        url,
                        timeout_s=_PER_MIRROR_TIMEOUT_S,
                        connect_s=_CONNECT_TIMEOUT_S,
                    ),
                    timeout=_PER_MIRROR_TIMEOUT_S,
                )
            except TimeoutError:
                last_err = TimeoutError(
                    f"{mirror} did not respond in {_PER_MIRROR_TIMEOUT_S:.0f}s"
                )
                log.debug("zlibrary mirror %s timed out", mirror)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("zlibrary mirror %s fetch failed: %s", mirror, e)

            if html is not None:
                hits = self._parse(html, mirror, limit)
                if hits:
                    return hits

            # Browser fallback: retry through the Playwright pool for mirrors
            # that returned a Cloudflare challenge / JS-gated page.
            try:
                rendered = await self.fetcher.render(url)
                hits = self._parse(rendered, mirror, limit)
                if hits:
                    return hits
            except Exception as e:
                log.debug("zlibrary mirror %s render failed: %s", mirror, e)

            if html is not None:
                log.debug(
                    "zlibrary mirror %s returned 0 hits (likely Cloudflare challenge)", mirror
                )

        if last_err is not None:
            raise last_err
        return []

    def _resolve_mirrors(self) -> list[str]:
        """Honour user mirror config but always preserve a working fallback.

        Z-Library reshuffles its public clear-net domains frequently, so
        even a recently edited config can go stale fast.  We dedupe and
        append any default mirror the user is missing.
        """
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

    # ------------------------------------------------------------------
    # Parser
    # ------------------------------------------------------------------
    def _parse(self, html: str, base: str, limit: int) -> list[SearchHit]:
        # Strip HTML comments — Z-Library sometimes wraps cards in
        # commented-out blocks for paginated/server-cached responses.
        cleaned = html.replace("<!--", "").replace("-->", "")
        tree = HTMLParser(cleaned)

        cards = tree.css("z-bookcard")
        if not cards:
            return []

        out: list[SearchHit] = []
        seen: set[str] = set()
        for i, card in enumerate(cards):
            if len(out) >= limit:
                break
            href = (card.attributes.get("href") or "").strip()
            if not href:
                continue
            book_id = (card.attributes.get("id") or "").strip()
            key = book_id or href
            if key in seen:
                continue
            seen.add(key)

            title = self._slot_text(card, "title")
            if not title:
                continue
            authors_raw = self._slot_text(card, "author")
            authors = [a.strip() for a in re.split(r"[,;]| & ", authors_raw) if a.strip()]

            year = _to_int(card.attributes.get("year"))
            language = (card.attributes.get("language") or "").strip() or None
            ext = (card.attributes.get("extension") or "").strip().lower() or None
            filesize = (card.attributes.get("filesize") or "").strip() or None
            publisher = (card.attributes.get("publisher") or "").strip() or None
            isbn = (card.attributes.get("isbn") or "").strip() or None

            landing = urljoin(base + "/", href)
            download = (card.attributes.get("download") or "").strip()
            pdf_url = urljoin(base + "/", download) if download and ext == "pdf" else None

            score = max(0.05, 0.9 - i * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title.strip(),
                    authors=authors,
                    year=year,
                    isbn=isbn or None,
                    container=publisher,
                    url=landing,
                    pdf_url=pdf_url,
                    score=score,
                    extra={
                        "zlib_id": book_id or None,
                        "language": language,
                        "ext": ext,
                        "filesize": filesize,
                        "mirror": base,
                        "download_path": download or None,
                    },
                )
            )
        return out

    @staticmethod
    def _slot_text(card_node, slot_name: str) -> str:
        """Return text from a child element with ``slot=<slot_name>``."""
        for child in card_node.css(f'[slot="{slot_name}"]'):
            text = child.text(strip=True)
            if text:
                return text
        return ""


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d{4}", value)
    return int(m.group(0)) if m else None
