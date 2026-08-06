"""Async HTTP fetcher with per-host rate-limiting, retries, and UA rotation.

The fetcher exposes high-level helpers (`get_text`, `get_bytes`, `get_json`, `post`)
plus a `render(url)` method that lazily delegates to a Playwright browser pool
for JS / Cloudflare-protected pages.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from documentcrawler.config import FetcherConfig
from documentcrawler.fetcher.pdf_resolve import (
    is_probably_html,
    pdf_urls_from_html,
    prioritize_pdf_urls,
)
from documentcrawler.storage.writer import looks_like_pdf
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class FetchResponse:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("latin-1", errors="replace")


class _RateLimiter:
    """Simple per-host token bucket. Async-safe."""

    def __init__(self, config: FetcherConfig):
        self._config = config
        self._lock = asyncio.Lock()
        self._next_allowed: dict[str, float] = {}

    def _interval(self, host: str) -> float:
        rate = self._config.rate_limits.get(host) or self._config.rate_limits.get(
            "default_rate", 2.0
        )
        rate = max(rate, 0.01)
        return 1.0 / rate

    async def acquire(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            interval = self._interval(host)
            next_t = self._next_allowed.get(host, 0.0)
            wait = max(0.0, next_t - now)
            self._next_allowed[host] = max(now, next_t) + interval
        if wait > 0:
            await asyncio.sleep(wait)


class Fetcher:
    """Single shared async HTTP client + lazy Playwright browser pool."""

    def __init__(
        self,
        config: FetcherConfig,
        *,
        timeout_s: int = 30,
        max_retries: int = 3,
    ):
        self._config = config
        self._timeout = httpx.Timeout(timeout_s, connect=min(15, timeout_s))
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._limiter = _RateLimiter(config)
        self._browser_pool = None
        self._unhealthy_until: dict[str, float] = {}
        self._failure_counts: dict[str, int] = {}

    def is_host_healthy(self, url: str) -> bool:
        """Check if target host is currently healthy or in a cooldown period."""
        host = self._host(url)
        if not host:
            return True
        until = self._unhealthy_until.get(host, 0.0)
        return time.monotonic() >= until

    def mark_host_failed(self, url: str, cooldown_s: float = 60.0) -> None:
        """Record a network/timeout failure for host; trigger cooldown if threshold reached."""
        host = self._host(url)
        if not host:
            return
        cnt = self._failure_counts.get(host, 0) + 1
        self._failure_counts[host] = cnt
        if cnt >= 3:
            self._unhealthy_until[host] = time.monotonic() + cooldown_s

    def mark_host_success(self, url: str) -> None:
        """Reset failure counter when a host responds successfully."""
        host = self._host(url)
        if host:
            self._failure_counts.pop(host, None)
            self._unhealthy_until.pop(host, None)

    async def __aenter__(self) -> Fetcher:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=self._timeout,
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._browser_pool is not None:
            await self._browser_pool.close()
            self._browser_pool = None

    def _ua(self) -> str:
        return random.choice(self._config.user_agents) if self._config.user_agents else "Mozilla/5.0"

    @staticmethod
    def _host(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | bytes | None = None,
        json_body: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        timeout: httpx.Timeout | float | None = None,
        max_retries: int | None = None,
        honor_retry_after: bool = True,
    ) -> FetchResponse:
        """Single request with rate-limiting + tenacity retries.

        Per-call overrides:

        ``timeout``       Override the default ``httpx.Timeout``.  Pass a
                          float for the total budget or an ``httpx.Timeout``
                          for fine-grained control.
        ``max_retries``   Override ``self._max_retries`` for this call.
                          Useful for fast mirror probes where the caller
                          has its own fall-back loop.
        ``honor_retry_after``
                          On a 429 response, sleep ``Retry-After`` before
                          re-raising for retry.  Set to ``False`` for
                          callers that want to handle 429s themselves.
        """
        client = await self._ensure_client()
        host = self._host(url)
        await self._limiter.acquire(host)
        merged_headers = {"User-Agent": self._ua()}
        if headers:
            merged_headers.update(headers)

        eff_timeout: httpx.Timeout | float
        if timeout is None:
            eff_timeout = self._timeout
        elif isinstance(timeout, httpx.Timeout):
            eff_timeout = timeout
        else:
            eff_timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))

        attempts = max_retries if max_retries is not None else self._max_retries

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max(1, attempts)),
            wait=wait_exponential_jitter(initial=1, max=8),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt:
                resp = await client.request(
                    method,
                    url,
                    headers=merged_headers,
                    params=params,
                    data=data if json_body is None else None,
                    json=json_body,
                    timeout=eff_timeout,
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server error {resp.status_code}", request=resp.request, response=resp
                    )
                if resp.status_code == 429 and honor_retry_after:
                    retry_after = float(resp.headers.get("Retry-After", "2") or 2)
                    await asyncio.sleep(min(retry_after, 30))
                    raise httpx.HTTPStatusError(
                        "rate limited", request=resp.request, response=resp
                    )
                # When honor_retry_after=False, callers handle 429 themselves
                # and we return the response as-is so they can inspect
                # headers and decide whether to retry.

                content = resp.content
                if max_bytes is not None and len(content) > max_bytes:
                    content = content[:max_bytes]
                return FetchResponse(
                    url=str(resp.url),
                    status=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()},
                    content=content,
                )

        raise FetchError("request retries exhausted", url=url)

    async def get(self, url: str, **kw: Any) -> FetchResponse:
        return await self._request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> FetchResponse:
        return await self._request("POST", url, **kw)

    async def get_bytes(self, url: str, **kw: Any) -> bytes:
        r = await self.get(url, **kw)
        if r.status >= 400:
            raise FetchError(f"http {r.status}", status=r.status, url=url)
        return r.content

    async def get_text(self, url: str, **kw: Any) -> str:
        r = await self.get(url, **kw)
        if r.status >= 400:
            raise FetchError(f"http {r.status}", status=r.status, url=url)
        return r.text

    async def probe_text(
        self,
        url: str,
        *,
        timeout_s: float = 5.0,
        connect_s: float = 3.0,
        **kw: Any,
    ) -> str:
        """Tight, single-attempt GET tailored for mirror probes.

        Searchers iterate through a list of shadow-library mirrors; the
        outer loop already gives us redundancy, so we don't want the
        inner request layer to retry — every retry on a dead mirror just
        burns budget that should go to the next mirror.  We also use a
        tighter connect timeout so DNS-blocked / TCP-blackholed hosts
        fail fast instead of waiting for the OS connect timeout.
        """
        timeout = httpx.Timeout(timeout_s, connect=connect_s)
        return await self.get_text(
            url, timeout=timeout, max_retries=1, honor_retry_after=False, **kw
        )

    async def get_json(self, url: str, **kw: Any) -> Any:
        r = await self.get(url, **kw)
        if r.status >= 400:
            raise FetchError(f"http {r.status}", status=r.status, url=url)
        import json
        return json.loads(r.text)

    async def render(self, url: str) -> str:
        """Render `url` with Playwright and return the resulting HTML.

        Lazily imports Playwright to avoid a hard dep when no source needs it.
        """
        if self._browser_pool is None:
            try:
                from documentcrawler.fetcher.browser import BrowserPool
            except ImportError as e:
                raise FetchError(
                    "Playwright not installed. Install with `pip install documentcrawler[browser]` "
                    "and run `playwright install chromium`."
                ) from e
            self._browser_pool = BrowserPool(user_agent=self._ua())
            await self._browser_pool.start()
        host = self._host(url)
        await self._limiter.acquire(host)
        return await self._browser_pool.fetch_html(url)

    async def download_document(self, url: str) -> bytes:
        """Return document bytes for a URL that may be a PDF, an HTML interstitial, or JS-gated.

        Tries plain HTTP first (follow redirects), parses HTML for embedded PDF /
        download links, then falls back to Playwright when available.
        """
        try:
            r = await self.get(url, max_retries=self._max_retries)
            if r.status < 400:
                if looks_like_pdf(r.content):
                    return r.content
                if is_probably_html(r.content):
                    for u in prioritize_pdf_urls(pdf_urls_from_html(r.text, r.url)):
                        try:
                            r2 = await self.get(u, max_retries=2)
                            if r2.status < 400 and looks_like_pdf(r2.content):
                                return r2.content
                        except Exception:
                            continue
        except Exception:
            pass

        return await self._download_document_browser(url)

    async def _download_document_browser(self, url: str) -> bytes:
        try:
            from documentcrawler.fetcher.browser import BrowserPool
        except ImportError as e:
            raise FetchError(
                "This download needs a browser (Cloudflare or JS interstitial). "
                "Install with `pip install documentcrawler[browser]` "
                "and run `playwright install chromium`.",
                url=url,
            ) from e
        if self._browser_pool is None:
            self._browser_pool = BrowserPool(user_agent=self._ua())
            await self._browser_pool.start()
        host = self._host(url)
        await self._limiter.acquire(host)
        read_s = float(self._timeout.read or 30.0)
        timeout_ms = min(120_000, max(30_000, int(read_s * 1000)))
        return await self._browser_pool.download_pdf(url, timeout_ms=timeout_ms)
