"""Unit tests for queue priority re-ranking."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery
from documentcrawler.priority import calculate_priority_score


def test_priority_score_calculation(tmp_path):
    db_path = tmp_path / "priority_test.db"
    db = Database(db_path)

    doc_id = db.add_query(
        DocumentQuery(
            doi="10.1000/456",
            title="Transformer Architecture Overview",
            authors=["Vaswani, Ashish"],
            year=2021,
        )
    )

    doc = db.get(doc_id)
    assert doc is not None

    score = calculate_priority_score(doc)
    # 50 (DOI) + 30 (title) + 20 (authors) + 20 (year) + 21 (recent year bonus) = 141
    assert score == 141

    updated = db.rerank_queue()
    assert updated == 1

    reranked_doc = db.get(doc_id)
    assert reranked_doc is not None
    assert reranked_doc.priority == score

    db.close()
