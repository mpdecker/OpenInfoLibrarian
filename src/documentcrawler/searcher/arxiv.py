"""arXiv metadata searcher (Atom XML)."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import extract_arxiv_id

log = get_logger(__name__)

_API = "https://export.arxiv.org/api/query"
_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


@register("arxiv")
class ArxivSearcher(Searcher):

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        # Direct id lookup if user pasted one
        arx = extract_arxiv_id(query)
        if arx:
            search_query = f"id:{arx}"
        elif kind == "author":
            search_query = f'au:"{_strip(query)}"'
        elif kind == "title":
            search_query = f'ti:"{_strip(query)}"'
        else:
            search_query = f'all:"{_strip(query)}"'

        params = {
            "search_query": search_query,
            "max_results": min(limit, 50),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        # Let exceptions propagate so MultiSearcher records them in its
        # per-source breakdown — silently returning [] hid real failures
        # like DNS errors and rate limits.
        xml = await self.fetcher.get_text(_API, params=params)

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log.debug("arxiv response was not valid XML: %s", e)
            return []

        out: list[SearchHit] = []
        for i, entry in enumerate(root.findall("a:entry", _NS)):
            score = max(0.05, 1.0 - i * 0.03)
            h = self._hit_from_entry(entry, score)
            if h:
                out.append(h)
        return out

    def _hit_from_entry(self, entry: ET.Element, score: float) -> SearchHit | None:
        title = (entry.findtext("a:title", default="", namespaces=_NS) or "").strip()
        if not title:
            return None
        summary = (entry.findtext("a:summary", default="", namespaces=_NS) or "").strip()
        published = (entry.findtext("a:published", default="", namespaces=_NS) or "").strip()
        year = None
        if published[:4].isdigit():
            year = int(published[:4])

        authors = [
            (a.findtext("a:name", default="", namespaces=_NS) or "").strip()
            for a in entry.findall("a:author", _NS)
        ]
        authors = [a for a in authors if a]

        entry_id = (entry.findtext("a:id", default="", namespaces=_NS) or "").strip()
        arx = extract_arxiv_id(entry_id)
        pdf_url = f"https://arxiv.org/pdf/{arx}.pdf" if arx else None

        # arXiv DOI (some papers have one)
        doi: str | None = None
        for el in entry.findall("arxiv:doi", _NS):
            doi = (el.text or "").strip() or None
            if doi:
                break

        return SearchHit(
            source=self.name,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            abstract=summary or None,
            url=entry_id or None,
            pdf_url=pdf_url,
            score=score,
            extra={"arxiv_id": arx},
        )


def _strip(text: str) -> str:
    return re.sub(r"[^\w\s\-:.]+", " ", text).strip()
