from __future__ import annotations

from types import SimpleNamespace

from documentcrawler.metadata.publication_resolver import (
    build_publication_queries,
    score_publication_hit,
)


def test_build_publication_queries_uses_partial_clues_and_repository_hints() -> None:
    query = SimpleNamespace(
        doi=None,
        isbn=None,
        title="Attention all you need",
        authors=["Vaswani"],
        year=2017,
        keywords=["transformer", "neural machine translation"],
        extra={"journal": "NeurIPS", "editor": "Guyon"},
    )

    queries = build_publication_queries(query)
    query_texts = {text for _, text in queries}

    assert ("title", "Attention all you need") in queries
    assert any("Vaswani" in text and "2017" in text for text in query_texts)
    assert any("NeurIPS" in text for text in query_texts)
    assert any("Guyon" in text for text in query_texts)
    assert any("transformer" in text for text in query_texts)


def test_score_publication_hit_prefers_better_title_author_year_and_venue_match() -> None:
    query = SimpleNamespace(
        doi=None,
        isbn=None,
        title="Attention is all you need",
        authors=["Ashish Vaswani"],
        year=2017,
        keywords=["transformer"],
        extra={"journal": "NeurIPS"},
    )
    strong = SimpleNamespace(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
        doi="10.5555/good",
        isbn=None,
        container="NeurIPS",
        extra={},
        score=0.92,
    )
    weak = SimpleNamespace(
        title="Graph attention networks",
        authors=["Petar Velickovic"],
        year=2018,
        doi="10.5555/weak",
        isbn=None,
        container="ICLR",
        extra={},
        score=0.95,
    )

    assert score_publication_hit(strong, query) > score_publication_hit(weak, query)


class _FakeMultiSearcher:
    """Stands in for MultiSearcher; returns canned merged hits."""

    hits: list = []

    def __init__(self, fetcher):
        self.fetcher = fetcher

    async def search(self, query):
        from types import SimpleNamespace
        return SimpleNamespace(
            runs=[],
            merged=list(_FakeMultiSearcher.hits),
        )


def _resolve_query(**over):
    base = dict(doi=None, isbn=None, title="Attention is all you need",
                authors=[], year=None, keywords=[], extra={})
    base.update(over)
    return SimpleNamespace(**base)


def test_resolver_rejects_title_that_disagrees_with_query(monkeypatch):
    # "asdf qwerty" must not adopt a random high-searcher-score hit.
    import asyncio
    from documentcrawler.metadata.publication_resolver import PublicationResolver
    from documentcrawler.searcher import aggregate

    _FakeMultiSearcher.hits = [SimpleNamespace(
        title="Seasonal feeding ecology of Baltic herring",
        authors=["A. Researcher"], year=2019, doi="10.5555/random",
        isbn=None, container="J. Random Stud.", extra={}, score=0.99, source="crossref",
        url=None, abstract=None,
    )]
    monkeypatch.setattr(aggregate, "MultiSearcher", _FakeMultiSearcher)
    resolved = asyncio.run(PublicationResolver(object()).resolve(
        _resolve_query(title="asdf qwerty zxcv")))
    assert resolved is None


def test_resolver_accepts_citation_like_query_for_right_paper(monkeypatch):
    import asyncio
    from documentcrawler.metadata.publication_resolver import PublicationResolver
    from documentcrawler.searcher import aggregate

    _FakeMultiSearcher.hits = [SimpleNamespace(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"], year=2017, doi="10.5555/att",
        isbn=None, container="NeurIPS", extra={}, score=0.9, source="crossref",
        url=None, abstract=None,
    )]
    monkeypatch.setattr(aggregate, "MultiSearcher", _FakeMultiSearcher)
    resolved = asyncio.run(PublicationResolver(object()).resolve(
        _resolve_query(
            title="Vaswani, A., et al. (2017). Attention Is All You Need. NeurIPS.")))
    assert resolved is not None
    assert resolved.title == "Attention Is All You Need"
