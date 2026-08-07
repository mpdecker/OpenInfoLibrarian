"""Unit tests for document deduplication and cluster merging."""

from __future__ import annotations

import pytest

from documentcrawler.db import Database
from documentcrawler.dedupe import find_duplicate_clusters
from documentcrawler.models import DocStatus, DocumentQuery


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dedupe.db")
    yield database
    database.close()


def test_find_duplicate_clusters_by_doi(db: Database):
    id1 = db.add_query(DocumentQuery(doi="10.1000/182", title="Paper One"))
    id2 = db.add_query(DocumentQuery(title="Paper One Dup"))

    # Force duplicate DOI directly in database for testing cluster detection
    db._conn.execute("UPDATE documents SET doi = '10.1000/182' WHERE id = ?", (id2,))

    clusters = find_duplicate_clusters(db)
    assert len(clusters) == 1
    assert {d.id for d in clusters[0]} == {id1, id2}


def test_find_duplicate_clusters_by_title_similarity(db: Database):
    id1 = db.add_query(DocumentQuery(title="Attention Is All You Need!"))
    id2 = db.add_query(DocumentQuery(title="Attention Is All You Need"))

    clusters = find_duplicate_clusters(db, threshold=0.8)
    assert len(clusters) == 1
    assert {d.id for d in clusters[0]} == {id1, id2}


def test_merge_documents(db: Database):
    id1 = db.add_query(DocumentQuery(title="Paper Version A", authors=["Vaswani, Ashish"]))
    id2 = db.add_query(DocumentQuery(title="Paper Version B", year=2017))
    db._conn.execute("UPDATE documents SET doi = '10.1000/v2' WHERE id = ?", (id2,))

    db.set_status(id2, DocStatus.DONE, file_path="/tmp/paper.pdf", sha256="abc123sha")

    merged = db.merge_documents(primary_id=id1, secondary_ids=[id2])
    assert merged.id == id1
    assert merged.doi == "10.1000/v2"
    assert merged.year == 2017
    assert merged.authors == ["Vaswani, Ashish"]
    assert merged.status == DocStatus.DONE
    assert merged.file_path == "/tmp/paper.pdf"
    assert merged.sha256 == "abc123sha"

    assert db.get(id2) is None
