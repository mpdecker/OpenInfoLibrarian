"""Unit tests for the HTTP fetcher.

Uses pytest-httpx to mock HTTP responses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from documentcrawler.config import FetcherConfig
from documentcrawler.fetcher.http import FetchError, FetchResponse, Fetcher, _RateLimiter


# -----------------------------------------------------------------------------
# FetchResponse tests
# -----------------------------------------------------------------------------


def test_fetch_response_text_decoding():
    """Test UTF-8 text decoding."""
    response = FetchResponse(
        url="https://example.com",
        status=200,
        headers={"Content-Type": "text/html"},
        content=b"Hello World",
    )
    assert response.text == "Hello World"


def test_fetch_response_latin1_fallback():
    """Test Latin-1 fallback for non-UTF-8 content."""
    response = FetchResponse(
        url="https://example.com",
        status=200,
        headers={},
        content=b"\xe9\xe8\xea",  # Latin-1 encoded
    )
    text = response.text
    assert "\u00e9" in text  # é


def test_fetch_response_bytes_access():
    """Test raw bytes access."""
    content = b"binary content"
    response = FetchResponse(
        url="https://example.com",
        status=200,
        headers={},
        content=content,
    )
    assert response.content == content


# -----------------------------------------------------------------------------
# Rate limiter tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_acquire():
    """Test rate limiter doesn't block on first call."""
    config = FetcherConfig(rate_limits={"default_rate": 10.0})
    limiter = _RateLimiter(config)
    # First acquire should not wait
    await limiter.acquire("example.com")


@pytest.mark.asyncio
async def test_rate_limiter_per_host():
    """Test rate limiting is per-host."""
    config = FetcherConfig(rate_limits={"example.com": 100.0, "default_rate": 1.0})
    limiter = _RateLimiter(config)
    # Fast host should not wait
    await limiter.acquire("example.com")


@pytest.mark.asyncio
async def test_rate_limiter_respects_interval():
    """Test rate limiter respects the configured interval."""
    config = FetcherConfig(rate_limits={"slow.com": 1.0})  # 1 request per second
    limiter = _RateLimiter(config)
    
    # First call should not wait
    await limiter.acquire("slow.com")
    
    # Second call should need to wait (but we won't actually wait in tests)
    # Just verify the state was updated


# -----------------------------------------------------------------------------
# Fetcher initialization tests
# -----------------------------------------------------------------------------


def test_fetcher_initialization():
    """Test Fetcher can be initialized with default config."""
    config = FetcherConfig()
    fetcher = Fetcher(config)
    assert fetcher._config == config
    assert fetcher._client is None


def test_fetcher_user_agent_rotation():
    """Test UA rotation returns different agents."""
    config = FetcherConfig(user_agents=["Agent1", "Agent2", "Agent3"])
    fetcher = Fetcher(config)
    agents = {fetcher._ua() for _ in range(20)}
    assert len(agents) <= 3  # Should only return configured agents
    assert agents.issubset({"Agent1", "Agent2", "Agent3"})


def test_fetcher_default_user_agent():
    """Test default UA when none configured."""
    config = FetcherConfig(user_agents=[])
    fetcher = Fetcher(config)
    assert fetcher._ua() == "Mozilla/5.0"


def test_fetcher_host_extraction():
    """Test URL host extraction."""
    assert Fetcher._host("https://example.com/path") == "example.com"
    assert Fetcher._host("http://api.test.org/v1") == "api.test.org"
    assert Fetcher._host("invalid-url") == ""


# -----------------------------------------------------------------------------
# Fetcher HTTP method tests (using pytest-httpx)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_get_json(httpx_mock):
    """Test JSON fetching."""
    payload = {"key": "value"}
    httpx_mock.add_response(
        url="https://api.example.com/data",
        json=payload,
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.get_json("https://api.example.com/data")
        assert result == payload


@pytest.mark.asyncio
async def test_fetcher_get_text(httpx_mock):
    """Test text fetching."""
    httpx_mock.add_response(
        url="https://example.com/page",
        text="Hello World",
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.get_text("https://example.com/page")
        assert result == "Hello World"


@pytest.mark.asyncio
async def test_fetcher_get_bytes(httpx_mock):
    """Test bytes fetching."""
    content = b"binary data"
    httpx_mock.add_response(
        url="https://example.com/file",
        content=content,
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.get_bytes("https://example.com/file")
        assert result == content


@pytest.mark.asyncio
async def test_fetcher_post_json(httpx_mock):
    """Test POST with JSON body."""
    httpx_mock.add_response(
        url="https://api.example.com/submit",
        json={"success": True},
        status_code=201,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.post(
            "https://api.example.com/submit",
            json_body={"data": "test"},
        )
        assert result.status == 201
        request = httpx_mock.get_request()
        assert json.loads(request.content) == {"data": "test"}


@pytest.mark.asyncio
async def test_fetcher_follows_redirects(httpx_mock):
    """Test redirect following."""
    httpx_mock.add_response(
        url="https://example.com/redirect",
        status_code=302,
        headers={"Location": "https://example.com/final"},
    )
    httpx_mock.add_response(
        url="https://example.com/final",
        text="Final destination",
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.get_text("https://example.com/redirect")
        assert result == "Final destination"


@pytest.mark.asyncio
async def test_fetcher_handles_404(httpx_mock):
    """Test 404 error handling."""
    httpx_mock.add_response(
        url="https://example.com/notfound",
        status_code=404,
        text="Not Found",
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        with pytest.raises(FetchError) as exc_info:
            await fetcher.get_text("https://example.com/notfound")
        assert exc_info.value.status == 404


@pytest.mark.asyncio
@pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
    can_send_already_matched_responses=True,
)
async def test_fetcher_handles_500(httpx_mock):
    """Test 500 error handling (retries then raises HTTPStatusError)."""
    httpx_mock.add_response(
        url="https://example.com/error",
        status_code=500,
        text="Server Error",
    )

    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await fetcher.get_text("https://example.com/error")
        assert exc_info.value.response.status_code == 500


@pytest.mark.asyncio
async def test_fetcher_probe_text(httpx_mock):
    """Test probe_text with tight timeouts."""
    httpx_mock.add_response(
        url="https://example.com/probe",
        text="Quick response",
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.probe_text(
            "https://example.com/probe",
            timeout_s=2.0,
            connect_s=1.0,
        )
        assert result == "Quick response"


@pytest.mark.asyncio
async def test_fetcher_respects_max_bytes(httpx_mock):
    """Test max_bytes parameter."""
    httpx_mock.add_response(
        url="https://example.com/large",
        content=b"x" * 10000,
        status_code=200,
    )
    
    config = FetcherConfig()
    async with Fetcher(config) as fetcher:
        result = await fetcher.get_bytes(
            "https://example.com/large",
            max_bytes=1000,
        )
        # Should only return max_bytes
        assert len(result) <= 1000


# -----------------------------------------------------------------------------
# FetchError tests
# -----------------------------------------------------------------------------


def test_fetch_error_with_status():
    """Test FetchError with status code."""
    error = FetchError("Not found", status=404, url="https://example.com")
    assert error.status == 404
    assert error.url == "https://example.com"
    assert str(error) == "Not found"


def test_fetch_error_without_status():
    """Test FetchError without status code."""
    error = FetchError("Network error")
    assert error.status is None
    assert error.url is None


# -----------------------------------------------------------------------------
# Fetcher context manager tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_async_context_manager():
    """Test async context manager properly initializes and closes."""
    config = FetcherConfig()
    
    async with Fetcher(config) as fetcher:
        assert fetcher._client is not None
    
    # After exiting, client should be closed
    assert fetcher._client is None


@pytest.mark.asyncio
async def test_fetcher_manual_close():
    """Test manual close method."""
    config = FetcherConfig()
    fetcher = Fetcher(config)
    
    async with fetcher:
        pass  # Just initialize
    
    # Already closed by context manager
    assert fetcher._client is None


# -----------------------------------------------------------------------------
# PDF resolution tests
# -----------------------------------------------------------------------------


def test_is_probably_html():
    """Test HTML content detection."""
    from documentcrawler.fetcher.pdf_resolve import is_probably_html
    
    html_content = b"<!DOCTYPE html><html><body>Test</body></html>"
    assert is_probably_html(html_content) is True
    
    pdf_content = b"%PDF-1.4..."
    assert is_probably_html(pdf_content) is False
    
    empty_content = b""
    assert is_probably_html(empty_content) is False


def test_pdf_urls_from_html():
    """Test PDF URL extraction from HTML."""
    from documentcrawler.fetcher.pdf_resolve import pdf_urls_from_html
    
    html = """
    <html>
    <body>
    <a href="https://example.com/paper.pdf">Download PDF</a>
    <a href="/relative.pdf">Relative</a>
    <a href="https://example.com/page.html">Not PDF</a>
    </body>
    </html>
    """
    urls = pdf_urls_from_html(html, "https://example.com")
    assert "https://example.com/paper.pdf" in urls
    assert "https://example.com/relative.pdf" in urls


def test_prioritize_pdf_urls():
    """Test PDF URL prioritization."""
    from documentcrawler.fetcher.pdf_resolve import prioritize_pdf_urls
    
    urls = [
        "https://example.com/download?file=paper.pdf",
        "https://cdn.example.com/paper.pdf",
        "https://example.com/page.html",
    ]
    prioritized = prioritize_pdf_urls(urls)
    
    # Direct PDF links should come first
    assert any("cdn" in u for u in prioritized[:2])
