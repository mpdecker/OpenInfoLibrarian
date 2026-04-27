from datetime import UTC, datetime

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
