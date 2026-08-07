"""Unit tests for document annotation manager."""

from __future__ import annotations

import pytest

from documentcrawler.annotations import annotate_document
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery


def test_annotate_document(tmp_path):
    db_path = tmp_path / "annotation_test.db"
    db = Database(db_path)

    doc_id = db.add_query(DocumentQuery(title="Annotated Paper"))

    res = annotate_document(db, doc_id, rating=5, review_status="reviewed", notes="Essential reading")
    assert res["rating"] == 5
    assert res["review_status"] == "reviewed"
    assert res["notes"] == "Essential reading"

    fetched = db.get_annotation(doc_id)
    assert fetched is not None
    assert fetched["rating"] == 5
    assert fetched["notes"] == "Essential reading"

    # Test invalid rating
    with pytest.raises(ValueError, match="Rating must be an integer"):
        annotate_document(db, doc_id, rating=10)

    # Test invalid status
    with pytest.raises(ValueError, match="Invalid review status"):
        annotate_document(db, doc_id, review_status="invalid_status")

    db.close()
