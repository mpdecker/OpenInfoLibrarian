"""arXiv source: search the API by title/author and return PDF URLs."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from documentcrawler.models import Candidate
from documentcrawler.sources.base import Source, SourceContext, register
from documentcrawler.utils.sanitize import extract_arxiv_id

_API = "https://export.arxiv.org/api/query"
_NS = {"a": "http://www.w3.org/2005/Atom"}


@register("arxiv")
class ArxivSource(Source):

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        # 1) explicit arxiv id in metadata.raw or title
        arxiv_id = extract_arxiv_id(ctx.query.title or "") or extract_arxiv_id(
            ctx.query.url or ""
        )
        if arxiv_id:
            return [self._candidate_from_id(arxiv_id, confidence=0.99)]

        title = ctx.metadata.title or ctx.query.title
        if not title:
            return []
        author = (ctx.metadata.authors or ctx.query.authors or [""])[0]

        query_parts = [f'ti:"{_strip(title)}"']
        if author:
            query_parts.append(f'au:"{_strip(author)}"')
        params = {"search_query": " AND ".join(query_parts), "max_results": 5}

        try:
            xml = await ctx.fetcher.get_text(_API, params=params)
        except Exception:
            return []

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []

        candidates: list[Candidate] = []
        for entry in root.findall("a:entry", _NS):
            entry_id = (entry.findtext("a:id", default="", namespaces=_NS) or "").strip()
            entry_title = (entry.findtext("a:title", default="", namespaces=_NS) or "").strip()
            arx = extract_arxiv_id(entry_id)
            if not arx:
                continue
            confidence = 0.85
            if title and entry_title and title.strip().lower() in entry_title.lower():
                confidence = 0.95
            candidates.append(self._candidate_from_id(arx, confidence=confidence,
                                                     note=f"arxiv:{arx}"))
        return candidates

    @staticmethod
    def _candidate_from_id(arxiv_id: str, *, confidence: float, note: str | None = None) -> Candidate:
        return Candidate(
            source="arxiv",
            url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            confidence=confidence,
            note=note or f"arxiv:{arxiv_id}",
        )


def _strip(text: str) -> str:
    return re.sub(r"[^\w\s\-:]+", " ", text).strip()
