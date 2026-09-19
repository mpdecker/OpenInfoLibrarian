"""Enrichment field-precedence tests (query DOI vs fuzzy search results)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from documentcrawler.metadata.enricher import MetadataEnricher
from documentcrawler.models import DocumentQuery


_OPENALEX_RECORD = {
    "doi": "https://doi.org/10.48550/arXiv.1706.03762",
    "display_name": "Attention Is All You Need",
    "publication_year": 2017,
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {"display_name": "Noam Shazeer"}},
    ],
    "primary_location": {"pdf_url": "https://arxiv.org/pdf/1706.03762",
                         "source": {"display_name": "arXiv (Cornell University Library)"}},
    "locations": [],
    "ids": {},
}


@dataclass
class _FakeResolverHit:
    doi: str | None = "10.48550/arXiv.1706.03762"
    isbn: str | None = None
    title: str | None = "Attention Is All You Need (Reprint)"
    authors: list[str] = field(default_factory=lambda: ["Wrong Author"])
    year: int | None = 2025
    url: str | None = "https://wrong.example/1"
    journal: str | None = "Wrong Journal"
    publisher: str | None = "Wrong Publisher"
    source: str = "crossref_search"
    score: float = 0.6
    raw: dict[str, Any] = field(default_factory=dict)


_CROSSREF_RECORD = {
    "DOI": "10.48550/arXiv.1706.03762",
    "title": ["Attention Is All You Need"],
    "author": [{"given": "Ashish", "family": "Vaswani"}],
    "issued": {"date-parts": [[2017, 6]]},
    "URL": "https://doi.org/10.48550/arXiv.1706.03762",
    "container-title": ["Wrong Journal From Crossref"],
    "publisher": "Wrong Publisher",
}


class _FakeFetcher:
    """Answers OpenAlex and Crossref for the test DOI."""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url, params))
        if "api.openalex.org/works/doi:" in url:
            return _OPENALEX_RECORD
        if "api.crossref.org/works/" in url:
            return {"message": _CROSSREF_RECORD}
        raise RuntimeError(f"http 404 for {url}")


@pytest.fixture
def enricher(monkeypatch):
    e = MetadataEnricher(_FakeFetcher(), openalex_mailto="contact@example.com")
    async def _wrong_resolve(query):  # fuzzy title search gone wrong
        return _FakeResolverHit()
    monkeypatch.setattr(e.publication_resolver, "resolve", _wrong_resolve)
    return e


async def test_openalex_lookup_uses_polite_pool_mailto(enricher):
    q = DocumentQuery(doi="10.48550/arXiv.1706.03762")
    await enricher.enrich(q)
    oa_calls = [c for c in enricher.fetcher.calls if "openalex.org" in c[0]]
    assert oa_calls, "expected an OpenAlex DOI lookup"
    assert all(c[1] and c[1].get("mailto") == "contact@example.com" for c in oa_calls)


async def test_query_doi_outranks_resolver_guesses(enricher):
    q = DocumentQuery(doi="10.48550/arXiv.1706.03762", title="Attention Is All You Need")
    meta = await enricher.enrich(q)
    # Exact-DOI OpenAlex record must override the fuzzy resolver hit.
    assert meta.year == 2017
    assert meta.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert meta.title == "Attention Is All You Need"
    assert meta.journal and meta.journal != "Wrong Journal"
    # Both providers answered — regression guard: a NameError in either
    # _apply path would raise here (the mock-blind spot that let
    # https://documentcrawler.vercel.app serve empty metadata slip through).
    assert set(meta.enrich_providers) == {"crossref", "openalex"}


async def test_enricher_never_raises_on_partial_provider_data():
    # Resolver down, Crossref answering, OpenAlex raising: enrich must still
    # return a usable meta instead of throwing into the pipeline fallback.
    fetcher = _FakeFetcher()

    async def _dead_openalex(url, params=None):
        fetcher.calls.append((url, params))
        if "openalex.org" in url:
            raise RuntimeError("http 403")
        if "crossref.org" in url:
            return {"message": _CROSSREF_RECORD}
        raise RuntimeError(f"http 404 for {url}")

    fetcher.get_json = _dead_openalex  # type: ignore[method-assign]
    e = MetadataEnricher(fetcher)

    async def _wrong_resolve(query):
        return _FakeResolverHit()

    import pytest
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(e.publication_resolver, "resolve", _wrong_resolve)
        meta = await e.enrich(DocumentQuery(doi="10.48550/arXiv.1706.03762"))
    assert meta.enrich_providers == ["crossref"]
    assert meta.title == "Attention Is All You Need"


async def test_explicit_query_year_is_never_overridden(enricher):
    q = DocumentQuery(doi="10.48550/arXiv.1706.03762", year=2016)
    meta = await enricher.enrich(q)
    assert meta.year == 2016


_ARXIV_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <published>2017-06-12T17:57:34Z</published>
  </entry>
</feed>"""


class _ArxivFakeFetcher(_FakeFetcher):
    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        if "export.arxiv.org" in url:
            return _ARXIV_ATOM
        raise RuntimeError(f"http 404 for {url}")


async def test_arxiv_doi_alone_enriches_without_title():
    # DOI-only acquisition of an arXiv paper: Crossref/OpenAlex don't cover
    # DataCite DOIs, so the arXiv ID must be mined from the DOI itself.
    fetcher = _ArxivFakeFetcher()
    e = MetadataEnricher(fetcher)

    async def _none(query):
        return None

    import pytest
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(e.publication_resolver, "resolve", _none)
        meta = await e.enrich(DocumentQuery(doi="10.48550/arXiv.1706.03762"))
    assert meta.title == "Attention Is All You Need"
    assert meta.year == 2017
    assert "Ashish Vaswani" in meta.authors
    assert "https://arxiv.org/pdf/1706.03762.pdf" in meta.oa_urls
