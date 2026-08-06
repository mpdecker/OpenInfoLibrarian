"""Unit tests for FTS5 index optimization."""

from typer.testing import CliRunner

from documentcrawler.cli import app
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery

runner = CliRunner()


def test_db_optimize_fts(tmp_path):
    db_path = tmp_path / "fts_opt.db"
    db = Database(db_path)
    db.add_query(DocumentQuery(doi="10.1000/opt", title="Optimization Test"))
    db.optimize_fts()
    db.close()


def test_cli_db_optimize_fts(tmp_path):
    db_path = tmp_path / "fts_cli_opt.db"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[general]\ndownload_dir = "{tmp_path.as_posix()}"\ndb_path = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )

    db = Database(db_path)
    db.add_query(DocumentQuery(doi="10.1000/opt_cli", title="CLI Opt Test"))
    db.close()

    result = runner.invoke(app, ["-c", str(cfg_path), "db", "optimize-fts"])
    assert result.exit_code == 0
    assert "optimized FTS5" in result.output
