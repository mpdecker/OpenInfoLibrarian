"""Unit tests for metadata cleaner and HTML sanitization."""

from __future__ import annotations

import pytest

from documentcrawler.cleaner import clean_author_name, clean_authors, clean_title
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery


def test_clean_title():
    assert clean_title("<i>Attention</i> Is &quot;All&quot; You Need") == 'Attention Is "All" You Need'
    assert clean_title("Deep Learning<sub>v2</sub>  ") == "Deep Learningv2"
    assert clean_title(None) is None


def test_clean_author_name():
    assert clean_author_name("vaswani, ashish") == "Vaswani, Ashish"
    assert clean_author_name("Ashish Vaswani") == "Vaswani, Ashish"
    assert clean_author_name("<b>John</b> Doe") == "Doe, John"


def test_clean_authors():
    raw = ["vaswani, ashish", "Ashish Vaswani", "noam shazeer"]
    cleaned = clean_authors(raw)
    assert len(cleaned) == 2
    assert "Vaswani, Ashish" in cleaned
    assert "Shazeer, Noam" in cleaned


def test_db_clean_metadata(tmp_path):
    db_path = tmp_path / "clean_test.db"
    db = Database(db_path)

    doc_id = db.add_query(
        DocumentQuery(
            title="<i>Deep</i> Learning&amp; AI",
            authors=["john doe"],
            keywords=["<b>ai</b>"],
        )
    )

    cleaned_count = db.clean_metadata()
    assert cleaned_count == 1

    doc = db.get(doc_id)
    assert doc is not None
    assert doc.title == "Deep Learning& AI"
    assert doc.authors == ["Doe, John"]
    assert doc.keywords == ["ai"]

    db.close()
