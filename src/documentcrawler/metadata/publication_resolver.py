"""Resolve partial bibliographic clues into stronger publication metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Protocol


class QueryLike(Protocol):
    doi: str | None
    isbn: str | None
    title: str | None
    authors: list[str]
    year: int | None
    keywords: list[str]
    extra: dict[str, Any]


class HitLike(Protocol):
    source: str
    title: str | None
    authors: list[str]
    year: int | None
    doi: str | None
    isbn: str | None
    container: str | None
    extra: dict[str, Any]
    score: float
    url: str | None
    abstract: str | None


@dataclass(slots=True)
class ResolvedPublication:
    source: str
    score: float
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    journal: str | None = None
    publisher: str | None = None
    url: str | None = None
    abstract: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def build_publication_queries(query: QueryLike) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(kind: str, text: str | None) -> None:
        normalized = " ".join((text or "").split()).strip()
        if not normalized:
            return
        item = (kind, normalized)
        if item not in out:
            out.append(item)

    title = (query.title or "").strip()
    first_author = (query.authors[0] if query.authors else "").strip()
    year = str(query.year) if query.year else ""
    venue = _hint_first(query.extra, ("journal", "book", "booktitle", "container", "venue"))
    editor = _hint_first(query.extra, ("editor", "editors"))
    publisher = _hint_first(query.extra, ("publisher",))
    keywords = [k.strip() for k in query.keywords if k and k.strip()]

    add("doi", query.doi)
    add("isbn", query.isbn)
    add("title", title)
    add("author", first_author)
    add("auto", " ".join(part for part in (title, first_author, year) if part))
    add("auto", " ".join(part for part in (title, venue, year) if part))
    add("auto", " ".join(part for part in (title, editor) if part))
    add("auto", " ".join(part for part in (venue, first_author, year) if part))
    add("auto", " ".join(part for part in (publisher, title) if part))
    add("auto", " ".join(keywords[:4]))
    add("auto", " ".join(part for part in (title, " ".join(keywords[:3])) if part))
    return out[:8]


def score_publication_hit(hit: HitLike, query: QueryLike) -> float:
    score = max(0.0, float(getattr(hit, "score", 0.0))) * 20.0

    if query.doi and hit.doi and _norm(query.doi) == _norm(hit.doi):
        score += 200.0
    if query.isbn and hit.isbn and _norm(query.isbn) == _norm(hit.isbn):
        score += 200.0

    query_title = query.title or ""
    hit_title = hit.title or ""
    if query_title and hit_title:
        score += _text_similarity(query_title, hit_title) * 45.0

    query_author = query.authors[0] if query.authors else ""
    if query_author and hit.authors:
        score += _best_author_similarity(query_author, hit.authors) * 22.0

    if query.year and hit.year:
        if query.year == hit.year:
            score += 15.0
        elif abs(query.year - hit.year) == 1:
            score += 6.0

    venue = _hint_first(query.extra, ("journal", "book", "booktitle", "container", "venue"))
    if venue and hit.container:
        score += _text_similarity(venue, hit.container) * 12.0

    publisher = _hint_first(query.extra, ("publisher",))
    hit_publisher = hit.extra.get("publisher")
    if publisher and isinstance(hit_publisher, str):
        score += _text_similarity(publisher, hit_publisher) * 8.0

    if query.keywords:
        corpus = " ".join(
            part
            for part in (
                hit.title or "",
                " ".join(hit.authors or []),
                hit.container or "",
                getattr(hit, "abstract", None) or "",
            )
            if part
        ).lower()
        matches = sum(1 for kw in query.keywords if kw and kw.lower() in corpus)
        score += min(matches, 4) * 3.0

    return score


class PublicationResolver:
    def __init__(
        self,
        fetcher: Any,
        *,
        crossref_mailto: str | None = None,
        openalex_mailto: str | None = None,
        semantic_scholar_api_key: str | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.options_per_source: dict[str, dict[str, Any]] = {}
        if crossref_mailto:
            self.options_per_source["crossref"] = {"mailto": crossref_mailto}
        if openalex_mailto:
            self.options_per_source["openalex"] = {"mailto": openalex_mailto}
        if semantic_scholar_api_key:
            self.options_per_source["semantic_scholar"] = {
                "api_key": semantic_scholar_api_key,
            }

    async def resolve(self, query: QueryLike) -> ResolvedPublication | None:
        from documentcrawler.searcher.aggregate import MultiSearcher, SearchQuery

        searcher = MultiSearcher(self.fetcher)
        all_hits: list[Any] = []
        sources = _publication_sources(query)
        for kind, text in build_publication_queries(query):
            result = await searcher.search(
                SearchQuery(
                    text=text,
                    kind=kind,
                    sources=sources,
                    limit_per_source=5,
                    overall_timeout_s=10.0,
                    options_per_source=self.options_per_source,
                )
            )
            all_hits.extend(result.merged)

        if not all_hits:
            return None

        ranked: list[tuple[float, Any]] = [
            (score_publication_hit(hit, query), hit) for hit in all_hits
        ]
        ranked.sort(key=lambda item: item[0], reverse=True)
        best_score, best = ranked[0]
        if best_score < 25.0:
            return None

        return ResolvedPublication(
            source=best.source,
            score=best_score,
            title=best.title,
            authors=list(best.authors or []),
            year=best.year,
            doi=best.doi,
            isbn=best.isbn,
            journal=best.container,
            publisher=best.extra.get("publisher") if isinstance(best.extra, dict) else None,
            url=best.url,
            abstract=best.abstract,
            raw={
                "source_score": getattr(best, "score", 0.0),
                "contributors": best.extra.get("contributors") if isinstance(best.extra, dict) else None,
            },
        )


def _publication_sources(query: QueryLike) -> list[str]:
    sources = ["crossref", "openalex", "semantic_scholar", "openlibrary", "arxiv"]
    if query.isbn and "openlibrary" in sources:
        sources.remove("openlibrary")
        sources.insert(0, "openlibrary")
    return sources


def _hint_first(extra: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return None


def _text_similarity(a: str, b: str) -> float:
    a_norm = _norm(a)
    b_norm = _norm(b)
    if not a_norm or not b_norm:
        return 0.0
    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(ratio, overlap)


def _best_author_similarity(author: str, candidates: list[str]) -> float:
    return max((_text_similarity(author, candidate) for candidate in candidates), default=0.0)


def _norm(text: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return " ".join(cleaned.split())
