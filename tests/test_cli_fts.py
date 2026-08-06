"""Unit tests for search-fts CLI command."""

from typer.testing import CliRunner

from documentcrawler.cli import app
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery

runner = CliRunner()


def test_cli_search_fts(tmp_path):
    db_path = tmp_path / "fts_cli.db"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[general]\ndownload_dir = "{tmp_path.as_posix()}"\ndb_path = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )

    db = Database(db_path)
    db.add_query(DocumentQuery(doi="10.1000/fts_cli", title="Quantum Computing Advances"))
    db.close()

    result = runner.invoke(app, ["-c", str(cfg_path), "search-fts", "Quantum"])
    assert result.exit_code == 0
    assert "Quantum" in result.output
    assert "10.1000/fts_cli" in result.output
