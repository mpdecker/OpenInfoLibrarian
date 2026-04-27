"""OpenLibrary searcher (books, by ISBN/title/author)."""

from __future__ import annotations

import re
from typing import Any

from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_isbn

log = get_logger(__name__)

_API = "https://openlibrary.org/search.json"
_ISBN_API = "https://openlibrary.org/isbn/{isbn}.json"


@register("openlibrary")
class OpenLibrarySearcher(Searcher):

    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        # ISBN direct lookup
        isbn = normalize_isbn(query) if kind in ("auto", "isbn") else None
        if isbn:
            try:
                data = await self.fetcher.get_json(_ISBN_API.format(isbn=isbn))
            except Exception as e:
                log.debug("openlibrary isbn lookup failed: %s", e)
                data = None
            if isinstance(data, dict) and data.get("title"):
                hit = self._hit_from_book(data, score=1.0, isbn=isbn)
                if hit:
                    return [hit]

        params: dict[str, Any] = {"limit": min(limit, 50)}
        if kind == "author":
            params["author"] = query
        elif kind == "title":
            params["title"] = query
        else:
            params["q"] = query

        try:
            data = await self.fetcher.get_json(_API, params=params)
        except Exception as e:
            log.debug("openlibrary search failed: %s", e)
            return []

        docs = (data or {}).get("docs") or []
        out: list[SearchHit] = []
        for i, item in enumerate(docs):
            score = max(0.05, 1.0 - i * 0.03)
            isbns = item.get("isbn") or []
            best_isbn = next((normalize_isbn(x) for x in isbns if normalize_isbn(x)), None)
            authors = item.get("author_name") or []
            year = item.get("first_publish_year")
            url = None
            key = item.get("key")
            if key:
                url = f"https://openlibrary.org{key}"
            ia_id = (item.get("ia") or [None])[0]
            pdf_url = (
                f"https://archive.org/download/{ia_id}/{ia_id}.pdf" if ia_id else None
            )
            out.append(
                SearchHit(
                    source=self.name,
                    title=item.get("title"),
                    authors=authors,
                    year=year,
                    isbn=best_isbn,
                    container=(item.get("publisher") or [None])[0],
                    url=url,
                    pdf_url=pdf_url,
                    score=score,
                    extra={"ia_id": ia_id, "ebook": item.get("has_fulltext")},
                )
            )
        return out

    def _hit_from_book(
        self, data: dict[str, Any], *, score: float, isbn: str | None
    ) -> SearchHit | None:
        if not data.get("title"):
            return None
        publish_year: int | None = None
        if (date := data.get("publish_date")):
            m = re.search(r"(\d{4})", str(date))
            if m:
                publish_year = int(m.group(1))
        # author refs are /authors/OLxxxxA — we don't follow them for cost reasons.
        author_count = len(data.get("authors") or [])
        return SearchHit(
            source=self.name,
            title=data.get("title"),
            authors=[f"({author_count} author{'s' if author_count != 1 else ''})"]
            if author_count
            else [],
            year=publish_year,
            isbn=isbn,
            container=(data.get("publishers") or [None])[0],
            url=f"https://openlibrary.org{data.get('key', '')}" if data.get("key") else None,
            score=score,
            extra={"pages": data.get("number_of_pages")},
        )
