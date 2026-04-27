"""Verify Pipeline emits the expected progress events to a callback."""

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


class _OkSource(Source):
    name = "ok"

    async def search(self, ctx: SourceContext) -> list[Candidate]:
        return [Candidate(source=self.name, url="https://example.com/x.pdf")]

    async def fetch(self, candidate: Candidate, ctx: SourceContext) -> bytes | None:
        return PDF_BYTES


@pytest.fixture
def patched_registry():
    original = dict(registry)
    registry["ok"] = lambda cfg: _OkSource(options=cfg.options)
    yield
    registry.clear()
    registry.update(original)


def test_progress_events(tmp_path: Path, patched_registry, monkeypatch):
    cfg = Config()
    cfg.general = GeneralConfig(
        download_dir=tmp_path / "dl",
        db_path=tmp_path / "db.sqlite",
        min_pdf_bytes=1024,
        workers=1,
    )
    cfg.sources_order = ["ok"]
    cfg.sources = {"ok": SourceConfig(name="ok", enabled=True)}

    db = Database(cfg.general.db_path)
    db.add_query(DocumentQuery(doi="10.1234/a"))
    db.add_query(DocumentQuery(doi="10.1234/b"))

    from documentcrawler.metadata import enricher as enricher_mod

    async def fake_enrich(self, q):
        return enricher_mod.EnrichedMetadata(doi=q.doi, title=q.title)

    monkeypatch.setattr(enricher_mod.MetadataEnricher, "enrich", fake_enrich)

    events: list[tuple[str, dict]] = []
    pipeline = Pipeline(cfg, db, workers=1, progress_cb=lambda e, p: events.append((e, p)))
    summary = asyncio.run(pipeline.run(db.pending_or_failed()))
    db.close()

    types = [e for e, _ in events]
    assert types[0] == "run_start"
    assert types[-1] == "run_done"
    assert types.count("doc_start") == 2
    assert types.count("doc_done") == 2
    assert summary.succeeded == 2
    final = events[-1][1]
    assert final["succeeded"] == 2
    assert final["per_source"].get("ok") == 2
