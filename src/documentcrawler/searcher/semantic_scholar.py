"""Semantic Scholar metadata searcher.

The public REST endpoint is heavily rate-limited (1 request / second
without an API key) and routinely returns 429.  We:

* Let exceptions propagate so ``MultiSearcher`` records them in the
  per-source breakdown — silently returning ``[]`` made it look like the
  searcher was broken even when the real problem was throttling.
* Add a polite ``User-Agent`` header so the server can identify us.
* Trim the result limit to what the public quota allows.

If you have an API key set ``[searcher.semantic_scholar].api_key`` (or
pass ``options={"api_key": ...}``) and the searcher will use it.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from documentcrawler.fetcher import FetchError
from documentcrawler.searcher.base import Searcher, SearchHit, register
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)

_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_DOI_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"

_FIELDS = (
    "title,authors,year,abstract,externalIds,venue,openAccessPdf,url,referenceCount"
)

_DEFAULT_HEADERS = {
    # The API explicitly asks scrapers to identify themselves; without this
    # they return 403 from some Cloudflare regions.
    "User-Agent": "documentcrawler/0.1 (+https://github.com/)",
    "Accept": "application/json",
}

# Anonymous tier is "1 request / second" but the limiter is sloppy and we
# routinely see 429s.  We do at most one targeted retry (honouring
# ``Retry-After``) before giving up — the outer ``MultiSearcher`` already
# enforces a per-source budget, so we don't want to burn it all in retries.
_MAX_RETRY_AFTER_S = 8.0


@register("semantic_scholar")
class SemanticScholarSearcher(Searcher):
    async def search(self, query: str, limit: int = 20, kind: str = "auto") -> list[SearchHit]:
        if not query or not query.strip():
            return []

        headers = dict(_DEFAULT_HEADERS)
        api_key = self.options.get("api_key")
        if api_key:
            headers["x-api-key"] = str(api_key)

        doi = normalize_doi(query) if kind in ("auto", "doi") else None
        if doi:
            data = await self._get_json_with_backoff(
                _DOI_API.format(doi=doi), {"fields": _FIELDS}, headers
            )
            return [h for h in [self._hit(data, score=1.0)] if h]

        # The search endpoint capped at 100 with key, 20 anonymously.
        capped = min(limit, 20 if not api_key else 100)
        params: dict[str, Any] = {
            "query": _clean_query(query),
            "limit": capped,
            "fields": _FIELDS,
        }
        data = await self._get_json_with_backoff(_API, params, headers)

        items = (data or {}).get("data") or []
        out: list[SearchHit] = []
        for i, item in enumerate(items):
            score = max(0.05, 1.0 - i * 0.04)
            h = self._hit(item, score)
            if h:
                out.append(h)
        if not out and items:
            log.debug("semscholar returned %d items but none parsed", len(items))
        return out

    async def _get_json_with_backoff(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
    ) -> Any:
        """Single-shot Semantic Scholar fetch with one Retry-After-aware retry.

        The default ``Fetcher`` retry policy retries 3x with exponential
        backoff on 429s, which routinely bursts past the 30 s per-source
        budget for nothing — every attempt hits the same 1 rps wall.  We
        instead do at most one extra attempt, sleeping for the server's
        suggested ``Retry-After`` (capped) before giving up.
        """
        for attempt in range(2):
            try:
                resp = await self.fetcher.get(
                    url,
                    params=params,
                    headers=headers,
                    max_retries=1,
                    honor_retry_after=False,
                )
            except httpx.HTTPStatusError as e:
                # Inner retry wrapper still raises 5xx; surface a clean error.
                raise FetchError(
                    f"semantic scholar transport error: {e}", url=url
                ) from e

            if resp.status == 429:
                if attempt == 0:
                    retry_after = float(resp.headers.get("Retry-After", "1") or 1)
                    await asyncio.sleep(min(retry_after, _MAX_RETRY_AFTER_S))
                    continue
                raise FetchError(
                    "rate limited (429); set [metadata.semantic_scholar].api_key for higher quota",
                    status=429, url=url,
                )

            if resp.status >= 400:
                raise FetchError(f"http {resp.status}", status=resp.status, url=url)

            try:
                return json.loads(resp.text)
            except json.JSONDecodeError as e:
                raise FetchError(
                    f"semantic scholar returned non-JSON response: {e}", url=url
                ) from e

        return None

    def _hit(self, item: dict[str, Any] | None, score: float) -> SearchHit | None:
        if not item or not item.get("title"):
            return None
        ext = item.get("externalIds") or {}
        doi = ext.get("DOI")
        oa = item.get("openAccessPdf") or {}
        return SearchHit(
            source=self.name,
            title=item.get("title"),
            authors=[a.get("name") for a in (item.get("authors") or []) if a.get("name")],
            year=item.get("year"),
            doi=doi,
            container=item.get("venue"),
            abstract=item.get("abstract"),
            url=item.get("url"),
            pdf_url=oa.get("url"),
            score=score,
            extra={"references": item.get("referenceCount")},
        )


def _clean_query(text: str) -> str:
    """Semantic Scholar's relevance ranker is happier with plain words —
    boolean operators, slashes and quotes routinely trigger 400 errors."""
    cleaned = re.sub(r"[^\w\s\-]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text.strip()
