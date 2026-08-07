"""Unit tests for PDF content FTS5 indexing and search."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.models import DocStatus, DocumentQuery


def test_pdf_content_fts_indexing_and_search(tmp_path):
    db_path = tmp_path / "pdf_fts_test.db"
    db = Database(db_path)

    doc_id = db.add_query(DocumentQuery(title="Quantum Computing Paper", doi="10.1000/123"))
    db.set_status(doc_id, DocStatus.DONE, file_path="quantum.pdf")

    db.index_pdf_content(doc_id, "This research paper explores quantum error correction and qubits.")

    hits = db.search_pdf_content("quantum")
    assert len(hits) == 1
    assert hits[0].id == doc_id
    assert hits[0].title == "Quantum Computing Paper"

    hits_empty = db.search_pdf_content("superconductor")
    assert len(hits_empty) == 0

    db.close()
