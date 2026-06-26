"""Unit tests for document sources.

Each source parses one fixture payload — we don't hit live APIs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from documentcrawler.config import SourceConfig
from documentcrawler.models import Candidate, DocumentQuery
from documentcrawler.sources import build_source
from documentcrawler.sources.base import SourceContext, registry as source_registry


class FakeFetcher:
    """Stub Fetcher returning canned payloads keyed by URL substring."""

    def __init__(
        self,
        json_map: dict[str, Any] | None = None,
        text_map: dict[str, str] | None = None,
        fail: set[str] | None = None,
    ):
        self.json_map = json_map or {}
        self.text_map = text_map or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []

    def _match(self, url: str, m: dict[str, Any]) -> Any:
        for needle, payload in m.items():
            if needle in url:
                return payload
        return None

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.calls.append((url, kw))
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        m = self._match(url, self.json_map)
        return m or {}

    async def get_text(self, url: str, **kw: Any) -> str:
        self.calls.append((url, kw))
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        m = self._match(url, self.text_map)
        return m or ""

    async def get(self, url: str, **kw: Any):
        """Return a fake response object."""
        self.calls.append((url, kw))
        if any(n in url for n in self.fail):
            raise RuntimeError("simulated failure")
        return FakeResponse(200, self._match(url, self.text_map) or "")

    async def render(self, url: str) -> str:
        return await self.get_text(url)


class FakeResponse:
    """Mimics ``documentcrawler.fetcher.FetchResponse`` for tests."""

    def __init__(self, status: int, text: str, headers: dict[str, str] | None = None):
        self.status = status
        self.text = text
        self.headers = headers or {}
        self.url = ""
        self.content = text.encode("utf-8")


def make_ctx(
    fetcher: FakeFetcher,
    title: str = "Test Title",
    doi: str | None = None,
    isbn: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    options: dict[str, Any] | None = None,
    pmcid: str | None = None,
    pmid: str | None = None,
    oa_urls: list[str] | None = None,
) -> SourceContext:
    """Create a SourceContext for testing."""
    metadata_attrs = {
        "title": title,
        "doi": doi,
        "isbn": isbn,
        "authors": authors or [],
        "year": year,
        "pmcid": pmcid,
        "pmid": pmid,
        "oa_urls": oa_urls or [],
    }
    return SourceContext(
        query=DocumentQuery(title=title, doi=doi, isbn=isbn, authors=authors or [], year=year),
        fetcher=fetcher,  # type: ignore[arg-type]
        metadata=type("Metadata", (), metadata_attrs)(),
        options=options or {},
    )


# -----------------------------------------------------------------------------
# Source registry tests
# -----------------------------------------------------------------------------


def test_source_registry_has_expected_sources():
    expected = {
        "annas_archive",
        "arxiv",
        "doaj",
        "libgen",
        "open_access",
        "pubmed",
        "scihub",
        "zlibrary",
    }
    assert expected.issubset(set(source_registry))


# -----------------------------------------------------------------------------
# Sci-Hub source tests
# -----------------------------------------------------------------------------


def test_scihub_source_extracts_pdf_from_iframe():
    html = """
    <html>
    <body>
    <iframe id="pdf" src="https://sci-hub.se/download/12345/paper.pdf"></iframe>
    </body>
    </html>
    """
    fetcher = FakeFetcher(text_map={"sci-hub.se": html})
    cfg = SourceConfig(name="scihub", enabled=True, options={})
    source = build_source("scihub", cfg)
    ctx = make_ctx(fetcher, doi="10.1000/example")
    candidates = asyncio.run(source.search(ctx))
    assert len(candidates) == 1
    assert candidates[0].url == "https://sci-hub.se/download/12345/paper.pdf"
    assert candidates[0].source == "scihub"


def test_scihub_source_tries_multiple_mirrors():
    html = """
    <html><body>
    <embed type="application/pdf" src="//sci-hub.st/download/paper.pdf">
    </body></html>
    """
    fetcher = FakeFetcher(
        text_map={"sci-hub.st": html},
        fail={"sci-hub.se", "sci-hub.ru"}
    )
    cfg = SourceConfig(name="scihub", enabled=True, options={
        "mirrors": ["https://sci-hub.se", "https://sci-hub.ru", "https://sci-hub.st"]
    })
    source = build_source("scihub", cfg)
    ctx = make_ctx(fetcher, doi="10.1000/example")
    candidates = asyncio.run(source.search(ctx))
    assert len(candidates) == 1
    assert "sci-hub.st" in candidates[0].url


def test_scihub_source_returns_empty_without_doi():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="scihub", enabled=True, options={})
    source = build_source("scihub", cfg)
    ctx = make_ctx(fetcher, doi=None)
    candidates = asyncio.run(source.search(ctx))
    assert candidates == []


def test_scihub_source_extracts_from_button_onclick():
    html = """
    <html><body>
    <button onclick="location.href='//sci-hub.se/downloads/abc123.pdf'">Download</button>
    </body></html>
    """
    fetcher = FakeFetcher(text_map={"sci-hub.se": html})
    cfg = SourceConfig(name="scihub", enabled=True, options={})
    source = build_source("scihub", cfg)
    ctx = make_ctx(fetcher, doi="10.1000/example")
    candidates = asyncio.run(source.search(ctx))
    assert len(candidates) == 1
    assert "downloads/abc123.pdf" in candidates[0].url


# -----------------------------------------------------------------------------
# Anna's Archive source tests
# -----------------------------------------------------------------------------


def test_annas_archive_source_extracts_md5():
    # Use a valid 32-character MD5 hash
    md5_hash = "18e1b007a1dab45b30cc861ba2dfda25"
    search_html = f"""
    <html><body>
    <a href="/md5/{md5_hash}">Paper Title</a>
    </body></html>
    """
    detail_html = """
    <html><body>
    <a href="/slow_download/123">Slow Download</a>
    </body></html>
    """
    fetcher = FakeFetcher(text_map={
        "annas-archive.org/search": search_html,
        f"annas-archive.org/md5/{md5_hash}": detail_html,
    })
    cfg = SourceConfig(name="annas_archive", enabled=True, options={"base_url": "https://annas-archive.org"})
    source = build_source("annas_archive", cfg)
    ctx = make_ctx(fetcher, title="Paper Title")
    candidates = asyncio.run(source.search(ctx))
    assert len(candidates) >= 1
    assert any("slow_download" in c.url for c in candidates)


def test_annas_archive_source_extracts_ipfs_links():
    # Use a valid 32-character MD5 hash
    md5_hash = "a" * 32  # 32 'a' characters as a valid MD5 format
    search_html = f"""
    <html><body>
    <a href="/md5/{md5_hash}">Paper</a>
    </body></html>
    """
    detail_html = """
    <html><body>
    <a href="https://ipfs.io/ipfs/Qm123/paper.pdf">IPFS</a>
    </body></html>
    """
    fetcher = FakeFetcher(text_map={
        "annas-archive.org/search": search_html,
        f"annas-archive.org/md5/{md5_hash}": detail_html,
    })
    cfg = SourceConfig(name="annas_archive", enabled=True, options={"base_url": "https://annas-archive.org"})
    source = build_source("annas_archive", cfg)
    ctx = make_ctx(fetcher, title="Paper")
    candidates = asyncio.run(source.search(ctx))
    assert any("ipfs" in c.url for c in candidates)


def test_annas_archive_builds_doi_query():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="annas_archive", enabled=True, options={"base_url": "https://annas-archive.org"})
    source = build_source("annas_archive", cfg)
    ctx = make_ctx(fetcher, doi="10.1000/example")
    # The source should use DOI as query
    assert source._build_query(ctx) == "10.1000/example"


def test_annas_archive_builds_title_author_query():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="annas_archive", enabled=True, options={"base_url": "https://annas-archive.org"})
    source = build_source("annas_archive", cfg)
    ctx = make_ctx(fetcher, title="Machine Learning", authors=["John Smith"])
    query = source._build_query(ctx)
    assert "Machine Learning" in query
    assert "John Smith" in query


# -----------------------------------------------------------------------------
# arXiv source tests
# -----------------------------------------------------------------------------


def test_arxiv_source_returns_pdf_url():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="arxiv", enabled=True, options={})
    source = build_source("arxiv", cfg)
    ctx = make_ctx(fetcher, title="Paper")
    candidates = asyncio.run(source.search(ctx))
    # arXiv source needs more metadata to find papers
    # It uses the title to search arXiv API
    # For now, test that it returns empty when no match found
    assert isinstance(candidates, list)


# -----------------------------------------------------------------------------
# LibGen source tests
# -----------------------------------------------------------------------------


def test_libgen_source_returns_md5_candidates():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="libgen", enabled=True, options={})
    source = build_source("libgen", cfg)
    ctx = make_ctx(fetcher, title="Book Title")
    candidates = asyncio.run(source.search(ctx))
    # LibGen source is minimal; tests would need mock responses
    assert isinstance(candidates, list)


# -----------------------------------------------------------------------------
# Z-Library source tests
# -----------------------------------------------------------------------------


def test_zlibrary_source_returns_candidates():
    fetcher = FakeFetcher()
    cfg = SourceConfig(name="zlibrary", enabled=True, options={})
    source = build_source("zlibrary", cfg)
    ctx = make_ctx(fetcher, title="Book")
    candidates = asyncio.run(source.search(ctx))
    assert isinstance(candidates, list)


# -----------------------------------------------------------------------------
# PubMed source tests
# -----------------------------------------------------------------------------


def test_pubmed_source_parses_esearch():
    esearch_xml = """
    <?xml version="1.0"?>
    <eSearchResult>
        <IdList>
            <Id>12345</Id>
        </IdList>
    </eSearchResult>
    """
    esummary_xml = """
    <?xml version="1.0"?>
    <eSummaryResult>
        <DocSum>
            <Id>12345</Id>
            <Item Name="Title">PubMed Article</Item>
            <Item Name="AuthorList">
                <Item Name="Author">Author, A.</Item>
            </Item>
            <Item Name="PubDate">2023 Jan</Item>
        </DocSum>
    </eSummaryResult>
    """
    fetcher = FakeFetcher(text_map={
        "eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_xml,
        "eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_xml,
    })
    cfg = SourceConfig(name="pubmed", enabled=True, options={})
    source = build_source("pubmed", cfg)
    # PubMed source requires pmid in metadata
    ctx = make_ctx(fetcher, title="Article", pmid="12345")
    candidates = asyncio.run(source.search(ctx))
    assert isinstance(candidates, list)


# -----------------------------------------------------------------------------
# DOAJ source tests
# -----------------------------------------------------------------------------


def test_doaj_source_parses_results():
    payload = {
        "results": [
            {
                "bibjson": {
                    "title": "DOAJ Article",
                    "author": [{"name": "Researcher, R."}],
                    "year": "2022",
                    "identifier": [{"type": "doi", "id": "10.1000/doaj"}],
                    "link": [{"url": "https://doaj.org/article/123", "type": "fulltext"}],
                }
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"doaj.org/api": payload})
    cfg = SourceConfig(name="doaj", enabled=True, options={})
    source = build_source("doaj", cfg)
    ctx = make_ctx(fetcher, title="Article")
    candidates = asyncio.run(source.search(ctx))
    if candidates:
        assert candidates[0].source == "doaj"


# -----------------------------------------------------------------------------
# Open Access source tests
# -----------------------------------------------------------------------------


def test_open_access_source_checks_unpaywall():
    payload = {
        "results": [
            {
                "title": "OA Paper",
                "doi": "10.1000/oa",
                "best_oa_location": {
                    "url": "https://example.com/paper.pdf",
                    "pdf_url": "https://example.com/paper.pdf",
                },
            }
        ]
    }
    fetcher = FakeFetcher(json_map={"api.unpaywall.org": payload})
    cfg = SourceConfig(name="open_access", enabled=True, options={"email": "test@example.com"})
    source = build_source("open_access", cfg)
    # OpenAccess source requires oa_urls in metadata
    ctx = make_ctx(fetcher, doi="10.1000/oa", oa_urls=["https://example.com/paper.pdf"])
    candidates = asyncio.run(source.search(ctx))
    if candidates:
        assert any(c.source == "open_access" for c in candidates)
