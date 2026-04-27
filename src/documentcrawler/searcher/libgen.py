"""LibGen search (shadow library) — books and scimag.

Notes on mirrors and HTML formats
---------------------------------
There are several active LibGen mirrors and they do not all serve the same
HTML.  As of 2026 we know about two layouts:

* Legacy `libgen.is` / `libgen.rs` — single ``<table class="c">`` with a
  fixed 9+ column row, MD5 lives in the title-cell anchor.
* Modern `libgen.li` / `libgen.gs` — Bootstrap markup with
  ``<table id="tablelibgen" class="table table-striped">``.  The MD5 is
  *only* in the trailing "Mirrors" cell anchors (``ads.php?md5=...`` or
  ``library.lol/main/...``) and the title sits in cell 0 alongside an
  ``edition.php`` link.

Both formats are now parsed.  Mirrors are tried in priority order and the
first one that returns hits wins; an empty result is treated like a soft
failure so we keep walking the list.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

if TYPE_CHECKING:
    from selectolax.parser import Node

log = get_logger(__name__)

# Mirrors are tried in priority order. ``libgen.li`` and ``libgen.gs`` are
# the most reliably reachable today; ``libgen.is`` / ``libgen.rs`` are
# kept as fall-backs but are frequently DNS-blocked by upstream ISPs.
_DEFAULT_MIRRORS = [
    "https://libgen.li",
    "https://libgen.gs",
    "https://libgen.is",
    "https://libgen.rs",
]

# Per-mirror network budget in seconds.  Most working mirrors respond
# within 3-4 s; DNS-blocked or TCP-blackholed ones hit the OS connect
# timeout (~15 s) so we use ``Fetcher.probe_text`` which forces a tight
# 3 s connect / 5 s total budget and disables retry — falling through
# to the next mirror is much cheaper than retrying a dead one.
_PER_MIRROR_TIMEOUT_S = 6.0
_CONNECT_TIMEOUT_S = 3.0

_MD5_RE = re.compile(r"md5=([0-9A-Fa-f]{32})")
_MD5_PATH_RE = re.compile(r"/(?:main|fiction|scimag|file)/([0-9A-Fa-f]{32})")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")


@register("libgen")
class LibgenSearcher(Searcher):
    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        if not query:
            return []
        mirrors = self._resolve_mirrors()

        doi = normalize_doi(query) if kind in ("auto", "doi") else None
        isbn = normalize_isbn(query) if kind in ("auto", "isbn") else None

        last_err: Exception | None = None
        for mirror in mirrors:
            mirror = mirror.rstrip("/")
            try:
                if doi:
                    url = f"{mirror}/scimag/?q={quote(doi)}"
                    html = await asyncio.wait_for(
                        self.fetcher.probe_text(
                            url,
                            timeout_s=_PER_MIRROR_TIMEOUT_S,
                            connect_s=_CONNECT_TIMEOUT_S,
                        ),
                        timeout=_PER_MIRROR_TIMEOUT_S,
                    )
                    hits = self._parse_scimag(html, mirror, limit)
                else:
                    # libgen.li renames the param to `req` *or* `q` on the
                    # /index.php endpoint; both are accepted server-side.
                    url = f"{mirror}/index.php?req={quote(isbn or query)}&res=25"
                    html = await asyncio.wait_for(
                        self.fetcher.probe_text(
                            url,
                            timeout_s=_PER_MIRROR_TIMEOUT_S,
                            connect_s=_CONNECT_TIMEOUT_S,
                        ),
                        timeout=_PER_MIRROR_TIMEOUT_S,
                    )
                    hits = self._parse_books(html, mirror, limit)
            except TimeoutError:
                last_err = TimeoutError(f"{mirror} did not respond in {_PER_MIRROR_TIMEOUT_S:.0f}s")
                log.debug("libgen mirror %s timed out", mirror)
                continue
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("libgen mirror %s failed: %s", mirror, e)
                continue
            if hits:
                return hits

        if last_err is not None:
            # Re-raise so MultiSearcher can surface the failure in the
            # per-source breakdown rather than silently reporting 0 hits.
            raise last_err
        return []

    def _resolve_mirrors(self) -> list[str]:
        """Honour the user's configured mirror list, but always make
        sure known-good fall-backs are reachable when the user's list is
        outdated (e.g. ``libgen.is``-only configs from older releases)."""
        configured = self.options.get("mirrors") or []
        configured = [str(m).rstrip("/") for m in configured if m]
        if not configured:
            return list(_DEFAULT_MIRRORS)

        # Dedupe while preserving order, then append any default mirror
        # the user is missing so a stale config still has a working host.
        seen: set[str] = set()
        ordered: list[str] = []
        for m in configured + _DEFAULT_MIRRORS:
            if m in seen:
                continue
            seen.add(m)
            ordered.append(m)
        return ordered

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    def _parse_books(self, html: str, base: str, limit: int) -> list[SearchHit]:
        tree = HTMLParser(html)

        # Modern Bootstrap layout (libgen.li, libgen.gs)
        modern = tree.css_first("#tablelibgen")
        if modern is not None:
            return self._parse_modern_books(modern, base, limit)

        # Legacy layout (libgen.is, libgen.rs)
        legacy = tree.css_first("table.c")
        if legacy is not None:
            return self._parse_legacy_books(legacy, base, limit)

        return []

    def _parse_modern_books(self, table: Node, base: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        seen: set[str] = set()
        rows = table.css("tr")
        for row in rows[1:]:  # skip header
            if len(out) >= limit:
                break
            cells = row.css("td")
            if len(cells) < 8:
                continue
            md5 = self._extract_md5(row)
            if not md5 or md5 in seen:
                continue
            seen.add(md5)

            title = self._extract_libgen_li_title(cells[0])
            authors_raw = cells[1].text(strip=True)
            publisher = cells[2].text(strip=True) or None
            year_text = cells[3].text(strip=True)
            year = self._parse_year(year_text)
            language = cells[4].text(strip=True) or None
            ext = cells[7].text(strip=True) or None

            score = max(0.05, 0.9 - len(out) * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title or None,
                    authors=[a.strip() for a in re.split(r"[,;]", authors_raw) if a.strip()],
                    year=year,
                    container=publisher,
                    url=urljoin(base + "/", f"ads.php?md5={md5}"),
                    score=score,
                    extra={
                        "md5": md5,
                        "kind": "book",
                        "language": language,
                        "ext": ext,
                    },
                )
            )
        return out

    def _parse_legacy_books(self, table: Node, base: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        seen: set[str] = set()
        for row in table.css("tr"):
            if len(out) >= limit:
                break
            cells = row.css("td")
            if len(cells) < 9:
                continue
            authors_cell = cells[1].text(strip=True)
            title_cell = cells[2]
            title_text = self._clean_title(title_cell)
            md5 = self._extract_md5(row)
            if not md5 or md5 in seen:
                continue
            seen.add(md5)
            publisher = cells[3].text(strip=True) or None
            year_text = cells[4].text(strip=True)
            year = self._parse_year(year_text)
            score = max(0.05, 0.9 - len(out) * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title_text or None,
                    authors=[a.strip() for a in re.split(r"[,;]", authors_cell) if a.strip()],
                    year=year,
                    container=publisher,
                    url=urljoin(base + "/", f"book/index.php?md5={md5}"),
                    score=score,
                    extra={"md5": md5, "kind": "book"},
                )
            )
        return out

    def _parse_scimag(self, html: str, base: str, limit: int) -> list[SearchHit]:
        tree = HTMLParser(html)
        out: list[SearchHit] = []
        # Try modern table first, then legacy `.catalog` table.
        rows = tree.css("#tablelibgen tr") or tree.css("table.catalog tr, .catalog tr")
        for row in rows:
            if len(out) >= limit:
                break
            cells = row.css("td")
            if len(cells) < 4:
                continue
            authors_cell = cells[0].text(strip=True)
            title_cell = cells[1]
            title_text = self._clean_title(title_cell)
            doi_match = _DOI_RE.search(title_cell.text())
            doi = doi_match.group(0).rstrip(".,;)") if doi_match else None

            year = None
            for c in cells[2:]:
                t = c.text(strip=True)
                m = re.match(r"^(?:19|20)\d{2}", t)
                if m:
                    year = int(m.group(0))
                    break

            mirror_link: str | None = None
            for a in row.css("a"):
                href = a.attributes.get("href") or ""
                if any(h in href for h in ("library.lol", "libgen.li", "sci-hub", "ads.php")):
                    mirror_link = urljoin(base + "/", href)
                    break

            md5 = self._extract_md5(row)
            score = max(0.05, 0.9 - len(out) * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title_text or None,
                    authors=[a.strip() for a in re.split(r"[,;]", authors_cell) if a.strip()],
                    year=year,
                    doi=doi,
                    url=mirror_link,
                    score=score,
                    extra={"kind": "scimag", **({"md5": md5} if md5 else {})},
                )
            )
        return out

    # ------------------------------------------------------------------
    # Tiny utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_md5(node: Node) -> str | None:
        for a in node.css("a"):
            href = a.attributes.get("href") or ""
            m = _MD5_RE.search(href) or _MD5_PATH_RE.search(href)
            if m:
                return m.group(1).lower()
        return None

    @staticmethod
    def _parse_year(text: str) -> int | None:
        m = re.search(r"(?:19|20)\d{2}", text or "")
        return int(m.group(0)) if m else None

    @staticmethod
    def _clean_title(cell: Node) -> str:
        # Strip the trailing edition-id badges that libgen.li sticks on titles
        # ("bl 6846495", "aa 83180341") and collapse whitespace.
        txt = cell.text(strip=True)
        txt = re.sub(r"[a-z]{1,2}\s*\d{6,}\s*$", "", txt, flags=re.IGNORECASE).strip()
        return re.sub(r"\s+", " ", txt)

    @staticmethod
    def _extract_libgen_li_title(cell: Node) -> str:
        """libgen.li glues the title together with edition-id badges and
        sometimes a DOI line.  Prefer the longest ``edition.php`` anchor
        that doesn't start with ``DOI:`` and doesn't look like an
        issue/volume header — falling back to a badge-stripped cell text.
        """
        candidates: list[str] = []
        for a in cell.css("a"):
            href = a.attributes.get("href") or ""
            if "edition.php" not in href:
                continue
            text = a.text(strip=True)
            if not text:
                continue
            if text.upper().startswith("DOI"):
                continue
            # Skip volume/issue stubs ("2020-apr 03 vol. 34 iss. 07")
            if re.search(r"\bvol\.|\biss\.|^\d{4}-", text, re.I):
                continue
            candidates.append(text)
        if candidates:
            candidates.sort(key=len, reverse=True)
            return re.sub(r"\s+", " ", candidates[0]).strip()
        return LibgenSearcher._clean_title(cell)
