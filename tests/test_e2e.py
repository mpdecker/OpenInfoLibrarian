"""End-to-end smoke tests gated behind @pytest.mark.e2e.

Run with: pytest -m e2e
Skip with:  pytest -m "not e2e"
"""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.e2e
def test_init_and_status(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    subprocess.run(
        [sys.executable, "-m", "documentcrawler", "init", "--config", str(config)],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )
    assert config.exists()
    db = tmp_path / "crawler.db"
    assert db.exists()

    result = subprocess.run(
        [sys.executable, "-m", "documentcrawler", "status", "--config", str(config)],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )
    assert "Documents by status" in result.stdout


@pytest.mark.e2e
def test_add_and_run_oa(tmp_path):
    config = tmp_path / "config.toml"
    subprocess.run(
        [sys.executable, "-m", "documentcrawler", "init", "--config", str(config)],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "documentcrawler", "add",
            "--config", str(config),
            "--doi", "10.1038/s41586-020-2649-2",
        ],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )
    assert "queued" in result.stdout.lower()

    result = subprocess.run(
        [
            sys.executable, "-m", "documentcrawler", "run",
            "--config", str(config),
            "--legit-only",
        ],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
        timeout=120,
    )
    assert "succeeded" in result.stdout.lower()


@pytest.mark.e2e
def test_search_enqueues(tmp_path):
    config = tmp_path / "config.toml"
    subprocess.run(
        [sys.executable, "-m", "documentcrawler", "init", "--config", str(config)],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "documentcrawler", "search",
            "--config", str(config),
            "machine learning",
            "--sources", "crossref",
            "--limit", "3",
            "--queue-top", "1",
        ],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
        timeout=60,
    )
    assert "Queued" in result.stdout or "Merged hits" in result.stdout


@pytest.mark.e2e
def test_import_bibtex(tmp_path):
    config = tmp_path / "config.toml"
    subprocess.run(
        [sys.executable, "-m", "documentcrawler", "init",
         "--config", str(config), "--with-examples"],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )

    bib = tmp_path / "examples" / "refs.bib"
    if not bib.exists():
        pytest.skip("No bundled refs.bib found")

    result = subprocess.run(
        [
            sys.executable, "-m", "documentcrawler", "import",
            "--config", str(config),
            str(bib),
        ],
        capture_output=True, encoding="utf-8", check=True, cwd=str(tmp_path),
    )
    assert "added" in result.stdout.lower()
