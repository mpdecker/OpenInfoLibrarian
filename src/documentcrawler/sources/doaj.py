"""DOAJ article search."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register

_BASE = "https://doaj.org/api/search/articles"


@register("doaj")
class DoajSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        if ctx.metadata.doi:
            query = f'doi:"{ctx.metadata.doi}"'
        else:
            title = ctx.metadata.title or ctx.query.title
            if not title:
                return []
            query = f'bibjson.title:"{title}"'

        url = f"{_BASE}/{quote(query)}?pageSize=5"
        try:
            data = await ctx.fetcher.get_json(url)
        except Exception:
            return []

        results = (data or {}).get("results") or []
        candidates: list[Candidate] = []
        for r in results:
            for link in ((r.get("bibjson") or {}).get("link") or []):
                if isinstance(link, dict) and link.get("url") and _is_pdf_link(link):
                    candidates.append(
                        Candidate(
                            source=self.name,
                            url=link["url"],
                            confidence=0.85,
                            note="doaj",
                        )
                    )
        return candidates


def _is_pdf_link(link: dict[str, Any]) -> bool:
    ct = (link.get("content_type") or "").lower()
    url = (link.get("url") or "").lower()
    typ = (link.get("type") or "").lower()
    return ct == "application/pdf" or url.endswith(".pdf") or typ == "fulltext"
