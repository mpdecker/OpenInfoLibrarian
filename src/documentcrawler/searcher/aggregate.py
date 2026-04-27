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


class MultiSearcher:
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

        runs = await asyncio.gather(*(run_one(s) for s in query.sources))
        merged = self._merge(runs)
        return SearchResult(query=query, runs=list(runs), merged=merged)

    def _merge(self, runs: list[SourceRun]) -> list[SearchHit]:
        """Dedupe by (DOI > ISBN > normalized title+year), preferring entries
        with a `pdf_url` and higher score."""
        bucket: dict[str, SearchHit] = {}
        for run in runs:
            for h in run.hits:
                key = h.dedupe_key()
                cur = bucket.get(key)
                if cur is None:
                    bucket[key] = h
                    continue
                # Prefer PDF availability, then higher score, then merge metadata.
                better = self._prefer(cur, h)
                worse = h if better is cur else cur
                # Backfill missing metadata from worse → better.
                better = self._backfill(better, worse)
                bucket[key] = better
        merged = list(bucket.values())
        merged.sort(key=lambda x: (x.has_pdf, x.score), reverse=True)
        return merged

    @staticmethod
    def _prefer(a: SearchHit, b: SearchHit) -> SearchHit:
        if a.has_pdf and not b.has_pdf:
            return a
        if b.has_pdf and not a.has_pdf:
            return b
        return a if a.score >= b.score else b

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
