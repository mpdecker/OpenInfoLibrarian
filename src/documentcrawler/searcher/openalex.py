"""OpenAlex metadata searcher.

OpenAlex has rich author + concept metadata and often surfaces direct OA PDFs.
"""

from __future__ import annotations

from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_API = "https://api.openalex.org/works"


@register("openalex")
class OpenAlexSearcher(Searcher):

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        params: dict[str, Any] = {"per-page": min(limit, 50)}
        mailto = self.options.get("mailto")
        if mailto:
            params["mailto"] = mailto

        doi = normalize_doi(query) if kind in ("auto", "doi") else None
        if doi:
            try:
                data = await self.fetcher.get_json(f"{_API}/doi:{doi}", params=params)
            except Exception as e:
                log.debug("openalex doi lookup failed: %s", e)
                return []
            return [h for h in [self._hit(data, 1.0)] if h]

        if kind == "author":
            params["search"] = query
            params["filter"] = f"authorships.author.display_name.search:{query}"
        else:
            params["search"] = query

        try:
            data = await self.fetcher.get_json(_API, params=params)
        except Exception as e:
            log.debug("openalex search failed: %s", e)
            return []

        results = (data or {}).get("results") or []
        out: list[SearchHit] = []
        for i, item in enumerate(results):
            score = max(0.05, 1.0 - i * 0.03)
            h = self._hit(item, score)
            if h:
                out.append(h)
        return out

    def _hit(self, item: dict[str, Any] | None, score: float) -> SearchHit | None:
        if not item:
            return None
        authorships = item.get("authorships") or []
        authors = [
            (a.get("author") or {}).get("display_name") for a in authorships if a.get("author")
        ]
        authors = [a for a in authors if a]
        doi = (item.get("doi") or "").replace("https://doi.org/", "") or None

        # Try to surface a direct PDF
        pdf_url: str | None = None
        best_oa = item.get("best_oa_location") or {}
        if best_oa.get("pdf_url"):
            pdf_url = best_oa["pdf_url"]
        else:
            primary = item.get("primary_location") or {}
            if primary.get("pdf_url"):
                pdf_url = primary["pdf_url"]
        # If we still have nothing, scan all locations.
        if not pdf_url:
            for loc in item.get("locations") or []:
                if loc.get("pdf_url"):
                    pdf_url = loc["pdf_url"]
                    break

        host = item.get("host_venue") or item.get("primary_location", {}).get("source") or {}
        return SearchHit(
            source=self.name,
            title=item.get("title") or item.get("display_name"),
            authors=authors,
            year=item.get("publication_year"),
            doi=doi,
            container=(host.get("display_name") if isinstance(host, dict) else None),
            abstract=_invert_abstract(item.get("abstract_inverted_index")),
            url=item.get("doi") or item.get("id"),
            pdf_url=pdf_url,
            score=score,
            extra={
                "type": item.get("type"),
                "is_oa": item.get("open_access", {}).get("is_oa"),
            },
        )


def _invert_abstract(idx: dict[str, list[int]] | None) -> str | None:
    if not idx:
        return None
    positions: dict[int, str] = {}
    for word, places in idx.items():
        for p in places:
            positions[p] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))
