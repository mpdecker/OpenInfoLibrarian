"""Unit tests for metadata health quality audit scanner."""

from __future__ import annotations

from documentcrawler.audit import run_metadata_health_audit
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery


def test_metadata_health_audit(tmp_path):
    db_path = tmp_path / "audit_test.db"
    db = Database(db_path)

    db.add_query(
        DocumentQuery(
            doi="10.1000/123",
            title="Complete Paper",
            authors=["Alice", "Bob"],
            year=2024,
        )
    )

    db.add_query(DocumentQuery(title="Incomplete Paper"))

    audit_res = run_metadata_health_audit(db)
    assert audit_res["total_documents"] == 2
    assert audit_res["with_doi"] == 1
    assert audit_res["with_year"] == 1
    assert audit_res["with_authors"] == 1
    assert audit_res["has_doi_pct"] == 50.0

    db.close()
