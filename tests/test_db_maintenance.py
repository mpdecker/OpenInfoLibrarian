"""Unit tests for Database maintenance, health metrics, and backups."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from documentcrawler.cli import app
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


def test_db_maintenance_methods(tmp_path: Path):
    db_path = tmp_path / "maint_test.db"
    db = Database(db_path)

    stats = db.get_db_stats()
    assert stats["integrity_ok"] is True
    assert stats["total_documents"] == 0
    assert stats["total_webhooks"] == 0

    db.vacuum()
    assert db.integrity_check() is True
    db.close()


def test_cli_db_status_and_vacuum(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\ndownload_dir = "{tmp_path.as_posix()}"\n'
        f'db_path = "{(tmp_path / "test.db").as_posix()}"\n'
    )
    runner = CliRunner()

    res_status = runner.invoke(app, ["--config", str(config_path), "db", "status"])
    assert res_status.exit_code == 0
    assert "Database Status" in res_status.output
    assert "Integrity:" in res_status.output

    res_vacuum = runner.invoke(app, ["--config", str(config_path), "db", "vacuum"])
    assert res_vacuum.exit_code == 0
    assert "vacuumed" in res_vacuum.output
