"""End-to-end pipeline test with a fake source registered ad-hoc."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from documentcrawler.config import Config, GeneralConfig, SourceConfig
from documentcrawler.db import Database
from documentcrawler.models import Candidate, DocumentQuery
from documentcrawler.pipeline import Pipeline
from documentcrawler.sources.base import Source, SourceContext, registry

PDF_BYTES = b"%PDF-1.4\n" + b"x" * 30000


class _FakeSource(Source):
    name = "fake"

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        return [Candidate(source=self.name, url="https://example.com/file.pdf")]

    async def fetch(self, candidate: Candidate, ctx: SourceContext) -> bytes | None:
        return PDF_BYTES


@pytest.fixture
def patched_registry():
    original = dict(registry)
    registry["fake"] = lambda cfg: _FakeSource(options=cfg.options)
    yield
    registry.clear()
    registry.update(original)


def _config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.general = GeneralConfig(
        download_dir=tmp_path / "dl",
        db_path=tmp_path / "x.db",
        filename_template="{first_author_last}_{year}_{title_slug}.{ext}",
        folder_template=None,
        workers=1,
        min_pdf_bytes=1024,
    )
    cfg.sources_order = ["fake"]
    cfg.sources = {"fake": SourceConfig(name="fake", enabled=True)}
    return cfg


def test_pipeline_runs_end_to_end(tmp_path: Path, patched_registry, monkeypatch):
    cfg = _config(tmp_path)
    db = Database(cfg.general.db_path)
    db.add_query(DocumentQuery(
        doi="10.1234/test", title="My Paper", authors=["Jane Doe"], year=2024,
    ))

    # Disable real metadata enrichment by patching the enricher to return raw query data.
    from documentcrawler.metadata import enricher as enricher_mod

    async def fake_enrich(self, query):
        return enricher_mod.EnrichedMetadata(
            doi=query.doi, title=query.title, authors=list(query.authors),
            year=query.year, isbn=query.isbn,
        )

    monkeypatch.setattr(enricher_mod.MetadataEnricher, "enrich", fake_enrich)

    pipeline = Pipeline(cfg, db, workers=1)
    docs = db.pending_or_failed()
    summary = asyncio.run(pipeline.run(docs))

    assert summary.total == 1
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.per_source.get("fake") == 1

    rows = db.list_documents()
    row = rows[0]
    assert row.status.value == "done"
    assert row.file_path
    assert Path(row.file_path).read_bytes()[:5] == b"%PDF-"
    assert row.sha256
    db.close()


def test_pipeline_marks_failed_when_no_source_succeeds(tmp_path: Path, monkeypatch):
    class _Empty(Source):
        name = "empty"

        async def search(self, ctx):
            return []

    cfg = Config()
    cfg.general = GeneralConfig(
        download_dir=tmp_path / "dl",
        db_path=tmp_path / "x.db",
        workers=1,
        min_pdf_bytes=1024,
    )
    cfg.sources_order = ["empty"]
    cfg.sources = {"empty": SourceConfig(name="empty", enabled=True)}

    original = dict(registry)
    registry["empty"] = lambda cfg_: _Empty(options=cfg_.options)
    try:
        db = Database(cfg.general.db_path)
        db.add_query(DocumentQuery(doi="10.1/zz"))

        from documentcrawler.metadata import enricher as enricher_mod

        async def fake_enrich(self, q):
            return enricher_mod.EnrichedMetadata(doi=q.doi, title=q.title)

        monkeypatch.setattr(enricher_mod.MetadataEnricher, "enrich", fake_enrich)

        pipeline = Pipeline(cfg, db, workers=1)
        summary = asyncio.run(pipeline.run(db.pending_or_failed()))
        assert summary.failed == 1
        assert summary.succeeded == 0
        assert db.list_documents()[0].status.value == "failed"
        db.close()
    finally:
        registry.clear()
        registry.update(original)
