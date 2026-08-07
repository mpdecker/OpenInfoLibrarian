"""Unit tests for reading list exporter."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery
from documentcrawler.reading_list import generate_reading_list


def test_generate_reading_list_markdown_and_html(tmp_path):
    db_path = tmp_path / "reading_list_test.db"
    db = Database(db_path)

    doc_id = db.add_query(
        DocumentQuery(
            doi="10.1000/789",
            title="Attention Is All You Need",
            authors=["Vaswani, Ashish"],
            year=2017,
            keywords=["cs.AI"],
        )
    )

    docs = db.list_documents(limit=10)
    assert len(docs) == 1

    md_content = generate_reading_list(docs, fmt="markdown")
    assert "# Research Library Reading List" in md_content
    assert "Attention Is All You Need" in md_content
    assert "Vaswani, Ashish" in md_content

    html_content = generate_reading_list(docs, fmt="html")
    assert "<!DOCTYPE html>" in html_content
    assert "Attention Is All You Need" in html_content
    assert "cs.AI" in html_content

    db.close()
