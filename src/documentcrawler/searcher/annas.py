"""Anna's Archive search (shadow library).

Anna's Archive is reachable on a rotating set of mirror domains; many ISPs
DNS-block ``annas-archive.org`` so we always try a list of known mirrors
in priority order and stop on the first one that returns parseable hits.

The catalog page wraps results in ``<!-- ... -->`` comments which is a
mild anti-scraping measure.  The legacy parser stripped them, but newer
mirrors no longer use that trick on the public search page; we strip
defensively either way.

The 2025+ markup uses Tailwind classes — each row is wrapped in
``div.js-aarecord-list-outer`` and the title anchor carries
``class="js-vim-focus"``.  Older mirrors emit one big anchor wrapping the
entire row with an ``<h3>`` title inside, which we still support.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser, Node

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

log = get_logger(__name__)

_DEFAULT_MIRRORS = [
    "https://annas-archive.org",
    "https://annas-archive.gl",
    "https://annas-archive.se",
    "https://annas-archive.li",
]

_PER_MIRROR_TIMEOUT_S = 6.0
_CONNECT_TIMEOUT_S = 3.0
_MD5_RE = re.compile(r"/md5/([0-9a-f]{32})")


@register("annas_archive")
class AnnasArchiveSearcher(Searcher):
    """Scrape Anna's Archive search results.

    Each hit's ``extra["md5"]`` carries the catalog ID.  Selecting a hit
    and queueing it will be picked up by the ``annas_archive`` Source
    which knows how to translate that into a download.
    """

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        if not query:
            return []

        mirrors = self._resolve_mirrors()

        last_err: Exception | None = None
        for mirror in mirrors:
            url = f"{mirror}/search?q={quote(query)}"
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
                last_err = TimeoutError(f"{mirror} did not respond in {_PER_MIRROR_TIMEOUT_S:.0f}s")
                log.debug("annas mirror %s timed out", mirror)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("annas mirror %s fetch failed: %s", mirror, e)

            if html is not None:
                hits = self._parse(html, mirror, limit)
                if hits:
                    return hits

            # Render fallback for JS-gated/Cloudflare mirrors
            try:
                rendered = await self.fetcher.render(url)
                hits = self._parse(rendered, mirror, limit)
                if hits:
                    return hits
            except Exception as e:
                log.debug("annas mirror %s render failed: %s", mirror, e)

            if html is not None:
                log.debug("annas mirror %s returned 0 hits (likely anti-scrape page)", mirror)

        if last_err is not None:
            raise last_err
        return []

    def _resolve_mirrors(self) -> list[str]:
        """Build the mirror list, augmenting stale user configs with the
        current known-good fall-backs so an outdated ``config.toml``
        listing only the (often DNS-blocked) ``.org`` host still works.
        """
        opts = self.options
        configured = opts.get("mirrors") or []
        configured = [str(m).rstrip("/") for m in configured if m]

        # Back-compat: legacy single-mirror knob `base_url`.
        legacy = opts.get("base_url")
        if legacy:
            legacy_str = str(legacy).rstrip("/")
            if legacy_str and legacy_str not in configured:
                configured.insert(0, legacy_str)

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
        # Anna's wraps results in <!-- ... --> comments to throttle scraping.
        cleaned = html.replace("<!--", "").replace("-->", "")
        tree = HTMLParser(cleaned)

        # Modern Tailwind layout (annas-archive.gl as of 2025+)
        modern = tree.css('a.js-vim-focus[href*="/md5/"]')
        if modern:
            return self._parse_modern(modern, base, limit)

        # Legacy single-anchor layout (annas-archive.org pre-2024)
        return self._parse_legacy(tree, base, limit)

    def _parse_modern(self, anchors: list[Node], base: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        seen: set[str] = set()
        for i, ta in enumerate(anchors):
            if len(out) >= limit:
                break
            href = ta.attributes.get("href") or ""
            md5_match = _MD5_RE.search(href)
            if not md5_match:
                continue
            md5 = md5_match.group(1).lower()
            if md5 in seen:
                continue
            seen.add(md5)

            title = ta.text(strip=True)[:300]

            # Walk up to the row container.  Anna's wraps each result in
            # a div with class "js-aarecord-list-outer" or, on slightly
            # older mirrors, in a flex row with a border-bottom util.
            container = self._find_row_container(ta)

            meta_lines = self._extract_meta_lines(container, exclude=title)
            year = self._extract_year(meta_lines)
            authors = self._extract_authors(meta_lines)
            doi = self._extract_doi(meta_lines)
            isbn = self._extract_isbn(meta_lines)

            score = max(0.05, 0.9 - i * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title,
                    authors=authors,
                    year=year,
                    doi=doi,
                    isbn=isbn,
                    url=urljoin(base + "/", href),
                    score=score,
                    extra={
                        "md5": md5,
                        "meta": " | ".join(meta_lines)[:300],
                        "mirror": base,
                    },
                )
            )
        return out

    def _parse_legacy(self, tree: HTMLParser, base: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        seen: set[str] = set()
        for i, a in enumerate(tree.css('a[href*="/md5/"]')):
            if len(out) >= limit:
                break
            href = a.attributes.get("href") or ""
            md5_match = _MD5_RE.search(href)
            if not md5_match:
                continue
            md5 = md5_match.group(1).lower()
            if md5 in seen:
                continue
            seen.add(md5)

            title_node = a.css_first("h3")
            title = (title_node.text(strip=True) if title_node else a.text(strip=True))[:300]
            if not title:
                continue

            meta_text = ""
            for div in a.css("div"):
                t = div.text(strip=True)
                if t and len(t) > len(meta_text) and t != title:
                    meta_text = t

            year = None
            ym = re.search(r"\b(?:19|20)\d{2}\b", meta_text)
            if ym:
                year = int(ym.group(0))

            authors: list[str] = []
            if "," in meta_text:
                tail = meta_text.rsplit(",", 1)[-1].strip()
                if tail and not re.search(r"\d{4}", tail) and len(tail) < 200:
                    authors = [tail]

            # Extract DOI/ISBN from meta_text
            doi_match = re.search(r"10\.\d{4,}/[^\s<>\"']+", meta_text)
            doi = normalize_doi(doi_match.group(0)) if doi_match else None
            isbn_match = re.search(r"(?:ISBN(?:-?13)?:?\s*)?(?:97[89][- ]?)?\d{9}[\dX]", meta_text, re.I)
            isbn = normalize_isbn(isbn_match.group(0)) if isbn_match else None

            score = max(0.05, 0.9 - i * 0.04)
            out.append(
                SearchHit(
                    source=self.name,
                    title=title,
                    authors=authors,
                    year=year,
                    doi=doi,
                    isbn=isbn,
                    url=urljoin(base + "/", href),
                    score=score,
                    extra={"md5": md5, "meta": meta_text[:300], "mirror": base},
                )
            )
        return out

    # ------------------------------------------------------------------
    # Tiny utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _find_row_container(node: Node) -> Node | None:
        """Climb up to the per-result wrapper.  Falls back to the deepest
        ancestor that holds at least 4 ``<div>`` descendants — that's
        always the row, never the title cell."""
        cur = node.parent
        for _ in range(8):
            if cur is None:
                return None
            cls = (cur.attributes.get("class") or "")
            if "js-aarecord-list-outer" in cls:
                return cur
            if "border-b" in cls and "flex" in cls:
                return cur
            cur = cur.parent
        # Fall-back: walk up until we hit a div with many descendants.
        cur = node.parent
        for _ in range(5):
            if cur is None:
                break
            if len(cur.css("div")) >= 4:
                return cur
            cur = cur.parent
        return node.parent

    @staticmethod
    def _extract_meta_lines(container: Node | None, exclude: str) -> list[str]:
        """Collect plausible metadata strings from the row container.

        Anna's modern layout puts the **authors** and **year/publisher**
        inside ``<a href="/search?q=...">`` tags (not divs), and the file
        path / language / size inside small leaf ``<div>`` elements.  We
        therefore scan both element types and keep short, distinct lines.
        """
        if container is None:
            return []
        seen_norm: set[str] = set()
        out: list[str] = []

        def add(text: str) -> None:
            text = " ".join(text.split())
            if not text or text == exclude or len(text) > 600:
                return
            key = text.lower()
            if key in seen_norm:
                return
            # Drop lines that are pure substrings of one we already kept.
            if any(key in existing for existing in seen_norm):
                return
            seen_norm.add(key)
            out.append(text)

        for a in container.css("a"):
            href = a.attributes.get("href") or ""
            if href.startswith("/md5/"):
                continue
            add(a.text(strip=True))

        for d in container.css("div"):
            # Skip wrapper divs that contain other divs — their text is
            # the concatenation of children we've already seen.
            if d.css_first("div") is not None:
                continue
            add(d.text(strip=True))

        return out

    @staticmethod
    def _extract_year(lines: list[str]) -> int | None:
        for line in lines:
            m = re.search(r"\b(?:19|20)\d{2}\b", line)
            if m:
                return int(m.group(0))
        return None

    @staticmethod
    def _extract_authors(lines: list[str]) -> list[str]:
        # Anna's authors line typically reads "First Last, First Last, …"
        # and is the longest comma-rich line that doesn't start with a
        # filename / size / language token.
        candidates = [
            line for line in lines
            if line.count(",") >= 1
            and not line.lower().startswith(("english", "chinese", "russian", "german", "french", "spanish"))
            and not re.match(r"^[\w/.\-]+\.(?:pdf|epub|djvu|mobi|azw3?)\b", line, re.I)
            and len(line) <= 400
        ]
        if not candidates:
            return []
        # Pick the one with the most commas and reasonable length per part.
        candidates.sort(key=lambda s: (s.count(","), -len(s)), reverse=True)
        chosen = candidates[0]
        parts = [p.strip() for p in chosen.split(",")]
        # Filter out tokens that look like sizes / years / langs.
        cleaned = [
            p for p in parts
            if p
            and not re.fullmatch(r"\d{4}", p)
            and not re.search(r"\b(?:MB|KB|GB|pdf|epub|djvu)\b", p, re.I)
            and not re.fullmatch(r"\d+(?:\.\d+)?\s*(?:MB|KB|GB)", p, re.I)
            and len(p) < 120
        ]
        return cleaned[:8]

    @staticmethod
    def _extract_doi(lines: list[str]) -> str | None:
        """Extract DOI from metadata lines."""
        doi_pattern = re.compile(r"10\.\d{4,}/[^\s<>\"']+")
        for line in lines:
            match = doi_pattern.search(line)
            if match:
                doi = normalize_doi(match.group(0))
                if doi:
                    return doi
        return None

    @staticmethod
    def _extract_isbn(lines: list[str]) -> str | None:
        """Extract ISBN from metadata lines."""
        isbn_pattern = re.compile(r"(?:ISBN(?:-?13)?:?\s*)?(?:97[89][- ]?)?\d{9}[\dX]", re.I)
        for line in lines:
            match = isbn_pattern.search(line)
            if match:
                isbn = normalize_isbn(match.group(0))
                if isbn:
                    return isbn
        return None
