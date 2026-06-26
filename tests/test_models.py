"""Unit tests for Pydantic data models."""

from __future__ import annotations

from datetime import datetime

import pytest

from documentcrawler.models import (
    Candidate,
    DocStatus,
    DocumentQuery,
    DocumentRow,
    SavedSearchRow,
    AttemptResult,
)


# -----------------------------------------------------------------------------
# DocumentQuery tests
# -----------------------------------------------------------------------------


def test_document_query_is_empty():
    q = DocumentQuery()
    assert q.is_empty()

    q = DocumentQuery(doi="10.1000/example")
    assert not q.is_empty()

    q = DocumentQuery(title="Paper Title")
    assert not q.is_empty()

    q = DocumentQuery(isbn="9781234567890")
    assert not q.is_empty()

    q = DocumentQuery(url="https://example.com")
    assert not q.is_empty()


def test_document_query_first_author_last():
    # "Last, First" format
    q = DocumentQuery(authors=["Smith, John"])
    assert q.first_author_last() == "Smith"

    # "First Last" format
    q = DocumentQuery(authors=["John Smith"])
    assert q.first_author_last() == "Smith"

    # Multiple parts
    q = DocumentQuery(authors=["John Michael Smith"])
    assert q.first_author_last() == "Smith"

    # Empty authors
    q = DocumentQuery()
    assert q.first_author_last() is None

    # Single name
    q = DocumentQuery(authors=["Cher"])
    assert q.first_author_last() == "Cher"


def test_document_query_extra_ignored():
    """Extra fields should be allowed and stored."""
    q = DocumentQuery(doi="10.1000/example", extra={"key": "value"})
    assert q.extra["key"] == "value"


def test_document_query_model_validation():
    q = DocumentQuery(doi="10.1000/example", title="Test")
    assert q.doi == "10.1000/example"
    assert q.title == "Test"


# -----------------------------------------------------------------------------
# Candidate tests
# -----------------------------------------------------------------------------


def test_candidate_creation():
    c = Candidate(source="test", url="https://example.com/paper.pdf")
    assert c.source == "test"
    assert c.url == "https://example.com/paper.pdf"
    assert c.confidence == 1.0
    assert not c.needs_browser


def test_candidate_with_optional_fields():
    c = Candidate(
        source="scihub",
        url="https://example.com/paper.pdf",
        confidence=0.9,
        title="Paper Title",
        note="via sci-hub",
        needs_browser=True,
        extra={"mirror": "sci-hub.se"},
    )
    assert c.confidence == 0.9
    assert c.title == "Paper Title"
    assert c.note == "via sci-hub"
    assert c.needs_browser
    assert c.extra["mirror"] == "sci-hub.se"


# -----------------------------------------------------------------------------
# AttemptResult tests
# -----------------------------------------------------------------------------


def test_attempt_result_creation():
    now = datetime.now()
    ar = AttemptResult(
        source="scihub",
        success=True,
        candidate_url="https://example.com/paper.pdf",
        http_status=200,
        bytes=1024000,
        started_at=now,
        finished_at=now,
    )
    assert ar.source == "scihub"
    assert ar.success
    assert ar.candidate_url == "https://example.com/paper.pdf"
    assert ar.http_status == 200
    assert ar.bytes == 1024000


def test_attempt_result_failure():
    now = datetime.now()
    ar = AttemptResult(
        source="scihub",
        success=False,
        error="404 Not Found",
        started_at=now,
        finished_at=now,
    )
    assert not ar.success
    assert ar.error == "404 Not Found"
    assert ar.candidate_url is None


# -----------------------------------------------------------------------------
# DocumentRow tests
# -----------------------------------------------------------------------------


def test_document_row_creation():
    now = datetime.now()
    dr = DocumentRow(
        id=1,
        doi="10.1000/example",
        title="Test Document",
        authors=["Author, A."],
        year=2023,
        status=DocStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    assert dr.id == 1
    assert dr.doi == "10.1000/example"
    assert dr.title == "Test Document"
    assert dr.authors == ["Author, A."]
    assert dr.year == 2023
    assert dr.status == DocStatus.PENDING


def test_document_row_status_enum():
    assert DocStatus.PENDING == "pending"
    assert DocStatus.IN_PROGRESS == "in_progress"
    assert DocStatus.DONE == "done"
    assert DocStatus.FAILED == "failed"


def test_document_row_optional_fields():
    now = datetime.now()
    dr = DocumentRow(
        id=2,
        isbn="9781234567890",
        keywords=["machine learning", "AI"],
        file_path="/downloads/paper.pdf",
        sha256="a" * 64,
        error=None,
        created_at=now,
        updated_at=now,
    )
    assert dr.isbn == "9781234567890"
    assert dr.keywords == ["machine learning", "AI"]
    assert dr.file_path == "/downloads/paper.pdf"
    assert dr.sha256 == "a" * 64


# -----------------------------------------------------------------------------
# SavedSearchRow tests
# -----------------------------------------------------------------------------


def test_saved_search_row_creation():
    now = datetime.now()
    ssr = SavedSearchRow(
        id=1,
        name="Machine Learning Papers",
        query_text="machine learning",
        kind="title",
        sources=["crossref", "openalex"],
        limit_per_source=20,
        created_at=now,
        updated_at=now,
    )
    assert ssr.id == 1
    assert ssr.name == "Machine Learning Papers"
    assert ssr.query_text == "machine learning"
    assert ssr.kind == "title"
    assert ssr.sources == ["crossref", "openalex"]
    assert ssr.limit_per_source == 20


def test_saved_search_row_defaults():
    now = datetime.now()
    ssr = SavedSearchRow(
        id=2,
        name="Default Search",
        query_text="test",
        created_at=now,
        updated_at=now,
    )
    assert ssr.kind == "auto"
    assert ssr.sources == []
    assert ssr.limit_per_source == 15


# -----------------------------------------------------------------------------
# Model serialization tests
# -----------------------------------------------------------------------------


def test_document_query_json_roundtrip():
    q = DocumentQuery(
        doi="10.1000/example",
        title="Test Title",
        authors=["Author, A."],
        year=2023,
    )
    json_str = q.model_dump_json()
    q2 = DocumentQuery.model_validate_json(json_str)
    assert q2.doi == q.doi
    assert q2.title == q.title
    assert q2.authors == q.authors
    assert q2.year == q.year


def test_candidate_json_roundtrip():
    c = Candidate(
        source="test",
        url="https://example.com",
        confidence=0.9,
        extra={"key": "value"},
    )
    json_str = c.model_dump_json()
    c2 = Candidate.model_validate_json(json_str)
    assert c2.source == c.source
    assert c2.url == c.url
    assert c2.confidence == c.confidence
    assert c2.extra == c.extra


def test_model_dict_conversion():
    q = DocumentQuery(doi="10.1000/example", title="Test")
    d = q.model_dump()
    assert d["doi"] == "10.1000/example"
    assert d["title"] == "Test"
    assert "extra" in d
