from datetime import UTC, datetime

import pytest

from documentcrawler.db import Database
from documentcrawler.models import AttemptResult, DocStatus, DocumentQuery


def test_add_and_dedupe(tmp_path):
    db = Database(tmp_path / "x.db")
    a = db.add_query(DocumentQuery(doi="10.1/x", title="T", extra={"journal": "Nature"}))
    b = db.add_query(DocumentQuery(doi="10.1/x", title="T"))
    assert a == b  # dedupe by doi
    rows = db.list_documents()
    assert len(rows) == 1
    assert rows[0].extra == {"journal": "Nature"}


def test_status_summary(tmp_path):
    db = Database(tmp_path / "x.db")
    db.add_query(DocumentQuery(doi="10.1/a"))
    db.add_query(DocumentQuery(doi="10.1/b"))
    docs = db.list_documents()
    db.set_status(docs[0].id, DocStatus.DONE, file_path="/tmp/a.pdf", sha256="abc")
    db.set_status(docs[1].id, DocStatus.FAILED, error="boom")
    counts = db.status_summary()
    assert counts.get("done") == 1
    assert counts.get("failed") == 1


def test_log_attempt_and_per_source_stats(tmp_path):
    db = Database(tmp_path / "x.db")
    doc_id = db.add_query(DocumentQuery(doi="10.1/a"))
    now = datetime.now(UTC)
    db.log_attempt(
        doc_id,
        AttemptResult(source="arxiv", success=True, started_at=now, finished_at=now),
    )
    db.log_attempt(
        doc_id,
        AttemptResult(source="arxiv", success=False, error="x", started_at=now, finished_at=now),
    )
    stats = db.per_source_stats()
    arx = next(s for s in stats if s["source"] == "arxiv")
    assert arx["successes"] == 1
    assert arx["failures"] == 1


def test_pending_or_failed(tmp_path):
    db = Database(tmp_path / "x.db")
    a = db.add_query(DocumentQuery(doi="10.1/a"))
    b = db.add_query(DocumentQuery(doi="10.1/b"))
    db.set_status(a, DocStatus.DONE, file_path="/x")
    db.set_status(b, DocStatus.FAILED)
    pending_or_failed = db.pending_or_failed()
    assert {d.id for d in pending_or_failed} == {b}
    only_failed = db.pending_or_failed(only_failed=True)
    assert {d.id for d in only_failed} == {b}


def test_find_by_sha256(tmp_path):
    db = Database(tmp_path / "x.db")
    a = db.add_query(DocumentQuery(doi="10.1/a"))
    db.set_status(a, DocStatus.DONE, file_path="/x", sha256="deadbeef")
    found = db.find_by_sha256("deadbeef")
    assert found and found.id == a


def test_database_health(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.health() is True
    db.close()
    assert db.health() is False


def test_busy_timeout_set(tmp_path):
    db = Database(tmp_path / "x.db")
    row = db._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 5000


def test_save_and_list_searches(tmp_path):
    db = Database(tmp_path / "x.db")
    sid = db.save_search("my search", "machine learning", kind="auto",
                         sources=["crossref", "arxiv"], limit_per_source=10)
    searches = db.list_saved_searches()
    assert len(searches) >= 1
    assert any(s.id == sid and s.name == "my search" for s in searches)


def test_get_saved_search(tmp_path):
    db = Database(tmp_path / "x.db")
    sid = db.save_search("test", "query text", kind="doi",
                         sources=["openalex"])
    s = db.get_saved_search(sid)
    assert s is not None
    assert s.name == "test"
    assert s.query_text == "query text"
    assert s.kind == "doi"
    assert "openalex" in s.sources


def test_get_saved_search_not_found(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.get_saved_search(99999) is None


def test_delete_saved_search(tmp_path):
    db = Database(tmp_path / "x.db")
    sid = db.save_search("to_delete", "q")
    db.delete_saved_search(sid)
    assert db.get_saved_search(sid) is None


def test_update_saved_search(tmp_path):
    db = Database(tmp_path / "x.db")
    sid = db.save_search("old", "old query")
    db.update_saved_search(sid, name="new_name", query_text="new query",
                           kind="isbn", sources=["crossref"], limit_per_source=5)
    s = db.get_saved_search(sid)
    assert s.name == "new_name"
    assert s.query_text == "new query"
    assert s.kind == "isbn"
    assert s.limit_per_source == 5


def test_update_saved_search_partial(tmp_path):
    db = Database(tmp_path / "x.db")
    sid = db.save_search("partial", "original", kind="auto",
                         sources=["arxiv"])
    db.update_saved_search(sid, query_text="updated")
    s = db.get_saved_search(sid)
    assert s.name == "partial"
    assert s.query_text == "updated"
    assert s.kind == "auto"
    assert "arxiv" in s.sources


def test_update_saved_search_not_found(tmp_path):
    db = Database(tmp_path / "x.db")
    with pytest.raises(ValueError, match="not found"):
        db.update_saved_search(99999, name="nope")


def test_autocomplete_from_saved_searches(tmp_path):
    db = Database(tmp_path / "x.db")
    db.save_search("ml", "machine learning with transformers")
    suggestions = db.get_autocomplete_suggestions("machine")
    assert any("machine learning" in s for s in suggestions)


def test_autocomplete_from_document_titles(tmp_path):
    db = Database(tmp_path / "x.db")
    doc_id = db.add_query(DocumentQuery(title="Deep Learning for NLP", doi="10.1/a"))
    db.set_status(doc_id, DocStatus.DONE, file_path="/tmp/fake.pdf")
    suggestions = db.get_autocomplete_suggestions("Deep")
    assert any("Deep Learning" in s for s in suggestions)


def test_retry_transaction_succeeds(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.retry_transaction(max_attempts=3) as conn:
        conn.execute("SELECT 1")


def test_versioned_migrations_apply(tmp_path):
    db = Database(tmp_path / "x.db")
    row = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    assert row is not None
    version = db._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 3


def test_error_kind_column_exists(tmp_path):
    db = Database(tmp_path / "x.db")
    cols = {
        row[1]
        for row in db._conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    assert "error_kind" in cols
