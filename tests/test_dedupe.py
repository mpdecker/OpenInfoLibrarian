"""Unit tests for enhanced document deduplication engine."""

from __future__ import annotations

import pytest

from documentcrawler.db import Database
from documentcrawler.dedupe import (
    composite_similarity,
    find_duplicate_clusters,
    select_primary_document,
    title_similarity,
)
from documentcrawler.models import DocStatus, DocumentQuery


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dedupe_enhanced.db")
    yield database
    database.close()


def test_title_similarity():
    sim = title_similarity("Attention Is All You Need", "Attention is all you need!")
    assert sim == 1.0

    sim2 = title_similarity("Deep Residual Learning for Image Recognition", "Deep Residual Learning for Vision")
    assert sim2 > 0.6


def test_find_duplicate_clusters_exact_doi(db: Database):
    id1 = db.add_query(DocumentQuery(doi="10.1000/182", title="Paper One"))
    id2 = db.add_query(DocumentQuery(title="Paper One Dup"))

    db._conn.execute("UPDATE documents SET doi = '10.1000/182' WHERE id = ?", (id2,))

    clusters = find_duplicate_clusters(db, strategy="exact")
    assert len(clusters) == 1
    assert clusters[0]["match_reason"] == "exact_doi"
    assert clusters[0]["primary"].id == id1
    assert clusters[0]["secondaries"][0].id == id2


def test_find_duplicate_clusters_fuzzy_composite(db: Database):
    id1 = db.add_query(DocumentQuery(title="Attention Is All You Need!", authors=["Vaswani, Ashish"], year=2017))
    id2 = db.add_query(DocumentQuery(title="Attention Is All You Need", authors=["Vaswani, Ashish"], year=2017))

    clusters = find_duplicate_clusters(db, threshold=0.8, strategy="fuzzy")
    assert len(clusters) == 1
    assert clusters[0]["match_reason"] == "fuzzy_composite"
    assert clusters[0]["confidence"] > 0.85


def test_select_primary_document_prioritizes_done_status(db: Database):
    id1 = db.add_query(DocumentQuery(title="Paper Version A"))
    id2 = db.add_query(DocumentQuery(title="Paper Version A"))

    db.set_status(id2, DocStatus.DONE, file_path="/tmp/done.pdf")

    d1 = db.get(id1)
    d2 = db.get(id2)
    assert d1 is not None and d2 is not None

    primary = select_primary_document([d1, d2])
    assert primary.id == id2
