"""``Fetcher.download_document`` behaviour (HTTP + HTML interstitials)."""

from __future__ import annotations

import pytest

from documentcrawler.config import FetcherConfig
from documentcrawler.fetcher import Fetcher, FetchResponse

_PDF = b"%PDF-1.4\n" + b"z" * 30000


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher(FetcherConfig(), timeout_s=30, max_retries=2)


@pytest.mark.asyncio
async def test_download_document_returns_direct_pdf(fetcher: Fetcher) -> None:
    async def fake_get(url: str, **kw: object) -> FetchResponse:
        return FetchResponse(url=url, status=200, headers={}, content=_PDF)

    fetcher.get = fake_get  # type: ignore[method-assign]

    out = await fetcher.download_document("https://example.com/doc.pdf")
    assert out == _PDF


@pytest.mark.asyncio
async def test_download_document_follows_pdf_link_in_html(fetcher: Fetcher) -> None:
    html = (
        b'<!DOCTYPE html><html><body>'
        b'<a href="https://cdn.example/out.pdf">x</a></body></html>'
    )

    async def fake_get(url: str, **kw: object) -> FetchResponse:
        if "landing" in url:
            return FetchResponse(url=url, status=200, headers={}, content=html)
        if "out.pdf" in url:
            return FetchResponse(url=url, status=200, headers={}, content=_PDF)
        return FetchResponse(url=url, status=404, headers={}, content=b"")

    fetcher.get = fake_get  # type: ignore[method-assign]

    async def boom_browser(u: str) -> bytes:
        raise AssertionError("should not need browser when HTML yields a PDF URL")

    fetcher._download_document_browser = boom_browser  # type: ignore[method-assign]

    out = await fetcher.download_document("https://ex/landing")
    assert out == _PDF
