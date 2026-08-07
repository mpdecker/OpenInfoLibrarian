"""Unit tests for topic auto-tagger."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery
from documentcrawler.tagger import classify_document_topics


def test_classify_document_topics(tmp_path):
    db_path = tmp_path / "tagger_test.db"
    db = Database(db_path)

    doc_id = db.add_query(
        DocumentQuery(
            title="Deep Learning with Transformer Neural Networks",
            keywords=["deep learning"],
        )
    )

    doc = db.get(doc_id)
    assert doc is not None

    topics = classify_document_topics(doc)
    assert "Computer Science & AI" in topics

    tagged_count = db.auto_tag_documents()
    assert tagged_count == 1

    updated_doc = db.get(doc_id)
    assert updated_doc is not None
    assert "Computer Science & AI" in updated_doc.keywords

    db.close()
