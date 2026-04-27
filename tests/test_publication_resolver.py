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
