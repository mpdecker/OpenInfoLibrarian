"""Unit tests for WAL checkpoint and compaction engine."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery


def test_db_checkpoint_wal(tmp_path):
    db_path = tmp_path / "compaction_test.db"
    db = Database(db_path)

    db.add_query(DocumentQuery(title="Compaction Test Paper"))
    res = db.checkpoint_wal()

    assert "busy" in res
    assert "log_pages" in res
    assert "checkpointed_pages" in res

    db.close()
