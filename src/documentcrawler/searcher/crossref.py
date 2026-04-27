"""Crossref metadata searcher."""

from __future__ import annotations

from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_API = "https://api.crossref.org/works"


@register("crossref")
class CrossrefSearcher(Searcher):
    """Search the Crossref REST API. ~150M scholarly works."""

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        params: dict[str, Any] = {"rows": min(limit, 50)}
        mailto = self.options.get("mailto")
        if mailto:
            params["mailto"] = mailto

        if kind == "doi" or (kind == "auto" and normalize_doi(query)):
            doi = normalize_doi(query) or query
            try:
                data = await self.fetcher.get_json(f"{_API}/{doi}")
            except Exception as e:
                log.debug("crossref doi lookup failed: %s", e)
                return []
            msg = data.get("message") if isinstance(data, dict) else None
            return [self._hit_from_msg(msg, score=1.0)] if msg else []

        if kind == "author":
            params["query.author"] = query
        elif kind == "title":
            params["query.title"] = query
        else:
            params["query.bibliographic"] = query

        try:
            data = await self.fetcher.get_json(_API, params=params)
        except Exception as e:
            log.debug("crossref search failed: %s", e)
            return []

        items = (((data or {}).get("message") or {}).get("items")) or []
        hits: list[SearchHit] = []
        for i, item in enumerate(items):
            score = max(0.05, 1.0 - i * 0.03)
            hit = self._hit_from_msg(item, score=score)
            if hit:
                hits.append(hit)
        return hits

    def _hit_from_msg(self, msg: dict[str, Any] | None, *, score: float) -> SearchHit | None:
        if not msg:
            return None
        title = (msg.get("title") or [None])[0]
        authors: list[str] = []
        for a in msg.get("author") or []:
            given = a.get("given") or ""
            family = a.get("family") or ""
            full = (given + " " + family).strip() or a.get("name") or ""
            if full:
                authors.append(full)
        year: int | None = None
        date_parts = (
            (msg.get("issued") or msg.get("published-online") or msg.get("published-print") or {}).get("date-parts")
            or [[None]]
        )
        if date_parts and date_parts[0] and date_parts[0][0]:
            try:
                year = int(date_parts[0][0])
            except (TypeError, ValueError):
                year = None
        doi = msg.get("DOI")
        container = (msg.get("container-title") or [None])[0]
        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            container=container,
            url=msg.get("URL") or (f"https://doi.org/{doi}" if doi else None),
            abstract=_strip_jats(msg.get("abstract")),
            score=score,
            extra={"type": msg.get("type"), "publisher": msg.get("publisher")},
        )


def _strip_jats(s: str | None) -> str | None:
    if not s:
        return None
    import re

    return re.sub(r"<[^>]+>", "", s).strip() or None
