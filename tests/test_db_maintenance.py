"""Unit tests for database maintenance (vacuum, check, backup)."""

from pathlib import Path

from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery


def test_db_vacuum_and_integrity_check(tmp_path: Path):
    db_path = tmp_path / "maint.db"
    db = Database(db_path)
    db.add_query(DocumentQuery(doi="10.1000/1", title="Paper 1"))

    assert db.integrity_check() is True

    db.vacuum()
    assert db.integrity_check() is True
    db.close()


def test_db_live_backup(tmp_path: Path):
    src_path = tmp_path / "source.db"
    backup_path = tmp_path / "backups" / "backup.db"

    db = Database(src_path)
    doc_id = db.add_query(DocumentQuery(doi="10.1000/backup", title="Backup Paper"))

    db.backup(backup_path)
    db.close()

    assert backup_path.exists()

    backup_db = Database(backup_path)
    doc = backup_db.get(doc_id)
    assert doc is not None
    assert doc.title == "Backup Paper"
    assert backup_db.integrity_check() is True
    backup_db.close()
