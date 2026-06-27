"""Multi-source aggregator with parallel execution + dedupe."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from documentcrawler.fetcher import Fetcher
from documentcrawler.searcher.base import SearchHit, build_searcher
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_SOURCE_WEIGHTS: dict[str, float] = {
    "crossref": 1.0,
    "openalex": 0.95,
    "semantic_scholar": 0.9,
    "doi_org": 0.9,
    "arxiv": 0.85,
    "core": 0.8,
    "openlibrary": 0.75,
    "scihub": 0.7,
    "annas_archive": 0.65,
    "libgen": 0.65,
    "zlibrary": 0.65,
    "jstor": 0.8,
    "library_of_congress": 0.75,
    "uk_national_archives": 0.65,
    "europeana": 0.7,
    "elsevier": 0.75,
    "springer": 0.75,
    "ieee": 0.75,
    "wiley": 0.75,
}


def _source_weight(source: str) -> float:
    return _SOURCE_WEIGHTS.get(source, 0.8)


@dataclass
class SearchQuery:
    text: str
    kind: str = "auto"  # auto | doi | isbn | title | author
    sources: list[str] = field(default_factory=list)
    limit_per_source: int = 15
    overall_timeout_s: float = 20.0
    options_per_source: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SourceRun:
    source: str
    hits: list[SearchHit]
    elapsed_s: float
    error: str | None = None


@dataclass
class SearchResult:
    query: SearchQuery
    runs: list[SourceRun]
    merged: list[SearchHit]

    @property
    def total_hits(self) -> int:
        return sum(len(r.hits) for r in self.runs)


def _combined_score(hit: SearchHit) -> float:
    weight = _source_weight(hit.source)
    return hit.score * weight


class MultiSearcher:
    STREAMING = True

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    async def search(
        self,
        query: SearchQuery,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> SearchResult:
        if not query.sources:
            return SearchResult(query=query, runs=[], merged=[])

        async def run_one(name: str) -> SourceRun:
            opts = query.options_per_source.get(name, {})
            searcher = build_searcher(name, self.fetcher, options=opts)
            if searcher is None:
                return SourceRun(source=name, hits=[], elapsed_s=0.0, error="unknown searcher")
            if progress_cb:
                progress_cb("searcher_start", {"source": name})
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            try:
                hits = await asyncio.wait_for(
                    searcher.search(query.text, limit=query.limit_per_source, kind=query.kind),
                    timeout=query.overall_timeout_s,
                )
            except TimeoutError:
                if progress_cb:
                    progress_cb("searcher_done", {"source": name, "count": 0, "error": "timeout"})
                return SourceRun(
                    source=name, hits=[], elapsed_s=loop.time() - t0, error="timeout"
                )
            except Exception as e:
                log.debug("searcher %s error: %s", name, e)
                if progress_cb:
                    progress_cb("searcher_done", {"source": name, "count": 0, "error": str(e)})
                return SourceRun(
                    source=name, hits=[], elapsed_s=loop.time() - t0, error=str(e)
                )
            elapsed = loop.time() - t0
            if progress_cb:
                progress_cb("searcher_done", {"source": name, "count": len(hits), "error": None})
            return SourceRun(source=name, hits=hits, elapsed_s=elapsed, error=None)

        tasks = [asyncio.ensure_future(run_one(s)) for s in query.sources]
        runs: list[SourceRun] = []

        if MultiSearcher.STREAMING:
            bucket: dict[str, SearchHit] = {}
            for coro in asyncio.as_completed(tasks):
                run = await coro
                runs.append(run)
                if run.hits:
                    self._stream_merge_into(run.hits, bucket)
                if progress_cb:
                    progress_cb("searcher_done", {"source": run.source, "count": len(run.hits), "error": run.error})
            merged = list(bucket.values())
            merged.sort(key=lambda x: (x.has_pdf, _combined_score(x)), reverse=True)
            return SearchResult(query=query, runs=runs, merged=merged)
        else:
            runs = list(await asyncio.gather(*tasks))
            merged = self._merge(runs)
            return SearchResult(query=query, runs=runs, merged=merged)

    def _stream_merge_into(self, hits: list[SearchHit], bucket: dict[str, SearchHit]) -> None:
        for h in hits:
            key = h.dedupe_key()
            cur = bucket.get(key)
            if cur is None:
                h.extra.setdefault("contributors", {h.source})
                h.extra.setdefault("duplicates", []).append({
                    "source": h.source,
                    "url": h.url,
                    "pdf_url": h.pdf_url,
                    "score": h.score,
                })
                bucket[key] = h
                continue
            better = MultiSearcher._prefer(cur, h)
            worse = h if better is cur else cur
            MultiSearcher._record_alt(better, worse)
            better = MultiSearcher._backfill(better, worse)
            bucket[key] = better

    def _merge(self, runs: list[SourceRun]) -> list[SearchHit]:
        """Dedupe by (DOI > ISBN > normalized title+year), preferring entries
        with a `pdf_url` and higher score."""
        bucket: dict[str, SearchHit] = {}
        for run in runs:
            for h in run.hits:
                key = h.dedupe_key()
                cur = bucket.get(key)
                if cur is None:
                    h.extra.setdefault("contributors", {h.source})
                    h.extra.setdefault("duplicates", []).append({
                        "source": h.source,
                        "url": h.url,
                        "pdf_url": h.pdf_url,
                        "score": h.score,
                    })
                    bucket[key] = h
                    continue
                # Prefer PDF availability, then higher score, then merge metadata.
                better = MultiSearcher._prefer(cur, h)
                worse = h if better is cur else cur
                MultiSearcher._record_alt(better, worse)
                # Backfill missing metadata from worse → better.
                better = MultiSearcher._backfill(better, worse)
                bucket[key] = better
        merged = list(bucket.values())
        merged.sort(key=lambda x: (x.has_pdf, _combined_score(x)), reverse=True)
        return merged

    @staticmethod
    def _prefer(a: SearchHit, b: SearchHit) -> SearchHit:
        if a.has_pdf and not b.has_pdf:
            return a
        if b.has_pdf and not a.has_pdf:
            return b
        sa = _combined_score(a)
        sb = _combined_score(b)
        if abs(sa - sb) > 0.001:
            return a if sa > sb else b
        # Tiebreaker: prefer hit with richer metadata, then higher source priority.
        if a._non_null_field_count != b._non_null_field_count:
            return a if a._non_null_field_count > b._non_null_field_count else b
        return a if _source_weight(a.source) >= _source_weight(b.source) else b

    @staticmethod
    def _backfill(better: SearchHit, worse: SearchHit) -> SearchHit:
        for field_name in ("title", "year", "doi", "isbn", "abstract", "container", "pdf_url", "url"):
            if not getattr(better, field_name) and getattr(worse, field_name):
                setattr(better, field_name, getattr(worse, field_name))
        if not better.authors and worse.authors:
            better.authors = list(worse.authors)
        # Track the contributing sources.
        contributors = better.extra.setdefault("contributors", {better.source})
        if isinstance(contributors, set):
            contributors.add(worse.source)
        return better

    @staticmethod
    def _record_alt(better: SearchHit, worse: SearchHit) -> None:
        alts: list[dict[str, Any]] = better.extra.setdefault("duplicates", [])
        alts.append({
            "source": worse.source,
            "url": worse.url,
            "pdf_url": worse.pdf_url,
            "score": worse.score,
        })
