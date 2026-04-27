"""Sanity tests for the bundled example reference files."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from documentcrawler.cli import _EXAMPLE_FILES, _write_examples
from documentcrawler.importers import parse_file


def test_all_bundled_examples_exist() -> None:
    pkg = files("documentcrawler.examples")
    for name in _EXAMPLE_FILES:
        assert pkg.joinpath(name).is_file(), f"missing bundled example: {name}"


def test_write_examples_copies_all(tmp_path: Path) -> None:
    written = _write_examples(tmp_path / "ex")
    assert {p.name for p in written} == set(_EXAMPLE_FILES)
    for p in written:
        assert p.exists() and p.stat().st_size > 0


def test_each_bundled_example_parses(tmp_path: Path) -> None:
    written = _write_examples(tmp_path / "ex")
    by_name = {p.name: p for p in written}
    for name in _EXAMPLE_FILES:
        queries = list(parse_file(by_name[name]))
        assert queries, f"{name} produced zero queries"
