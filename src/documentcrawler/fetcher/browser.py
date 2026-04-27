"""Lazy Playwright browser pool. Imported only when a source needs JS rendering."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]

from documentcrawler.fetcher.pdf_resolve import pdf_urls_from_html, prioritize_pdf_urls
from documentcrawler.storage.writer import looks_like_pdf


class BrowserPool:
    def __init__(self, user_agent: str | None = None, headless: bool = True):
        if async_playwright is None:
            raise ImportError("playwright is not installed")
        self._ua = user_agent
        self._headless = headless
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
            if self._ua:
                kwargs["user_agent"] = self._ua
            self._context = await self._browser.new_context(**kwargs)

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    async def fetch_html(self, url: str, *, wait_until: str = "networkidle",
                         timeout_ms: int = 30000) -> str:
        if self._context is None:
            await self.start()
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return await page.content()
        finally:
            await page.close()

    async def fetch_bytes(self, url: str, *, timeout_ms: int = 60000) -> bytes:
        """Use the browser context's request API so we share cookies / UA."""
        if self._context is None:
            await self.start()
        resp = await self._context.request.get(url, timeout=timeout_ms)
        return await resp.body()

    async def download_pdf(self, url: str, *, timeout_ms: int = 120000) -> bytes:
        """Navigate like a user, then return PDF bytes (direct response, linked PDF, or download)."""
        from documentcrawler.fetcher.http import FetchError

        if self._context is None:
            await self.start()

        page = await self._context.new_page()
        tmp_download: Path | None = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
            await page.wait_for_timeout(2500)

            if resp:
                try:
                    body = await resp.body()
                    if looks_like_pdf(body):
                        return body
                except Exception:
                    pass

            final = page.url
            html = await page.content()
            for u in prioritize_pdf_urls(pdf_urls_from_html(html, final)):
                try:
                    r = await self._context.request.get(u, timeout=timeout_ms)
                    if r.ok:
                        b = await r.body()
                        if looks_like_pdf(b):
                            return b
                except Exception:
                    continue

            # Click-style downloads (Z-Library / Anna's / generic).
            selectors = (
                'a[download]',
                'a[href$=".pdf"]',
                'a[href*="/dl/"]',
                'a[href*="get.php"]',
            )
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    async with page.expect_download(timeout=min(30_000, timeout_ms)) as dl:
                        await loc.click()
                    d = await dl.value
                    tmp_download = Path(await d.path())
                    data = tmp_download.read_bytes()
                    if looks_like_pdf(data):
                        return data
                except Exception:
                    continue

            raise FetchError(
                "Playwright could not obtain a PDF from this page "
                "(Cloudflare, login, or non-PDF format).",
                url=url,
            )
        finally:
            await page.close()
            if tmp_download is not None:
                with contextlib.suppress(OSError):
                    tmp_download.unlink(missing_ok=True)
