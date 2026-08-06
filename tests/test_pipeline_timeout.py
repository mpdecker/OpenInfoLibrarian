"""Tests for pipeline per-document timeout and error_kind."""

import asyncio

from documentcrawler.config import Config
from documentcrawler.db import Database
from documentcrawler.models import Candidate, DocStatus, DocumentQuery
from documentcrawler.pipeline import Pipeline


class SlowSource:
    """A source that sleeps forever when searching."""

    def __init__(self):
        self.name = "slow_mock"
        self.searched = False

    async def search(self, ctx):
        self.searched = True
        await asyncio.sleep(3600)
        return []

    async def fetch(self, candidate, ctx):
        return None


def test_per_doc_timeout_fires(tmp_path):
    async def _run():
        db = Database(tmp_path / "test.db")
        cfg = Config()
        cfg.sources_order = ["slow_mock"]
        cfg.sources["slow_mock"] = type(
            "SourceConfig", (), {"name": "slow_mock", "enabled": True, "options": {}}
        )()

        doc_id = db.add_query(DocumentQuery(title="Timeout Test Doc"))
        doc = db.get(doc_id)

        class DummyEnricher:
            async def enrich(self, q):
                from documentcrawler.metadata.enricher import EnrichedMetadata
                return EnrichedMetadata(doi=q.doi, title=q.title)

        pipeline = Pipeline(cfg, db, per_doc_timeout_s=0.5, workers=1)
        pipeline._source_instances = [SlowSource()]
        pipeline._build_enricher = lambda fetcher: DummyEnricher()

        summary = await pipeline.run([doc])

        assert summary.failed == 1
        updated = db.get(doc_id)
        assert updated.status == DocStatus.FAILED
        assert "timeout" in (updated.error or "").lower()

        attempts = db.attempts_for(doc_id)
        assert len(attempts) >= 1
        assert any(a.error_kind == "transient" for a in attempts if a.error_kind)
        db.close()

    asyncio.run(_run())


def test_cancel_event_checked_between_sources(tmp_path):
    async def _run():
        db = Database(tmp_path / "test.db")
        cfg = Config()

        class StubSource:
            def __init__(self, name):
                self.name = name
                self.search_called = False

            async def search(self, ctx):
                self.search_called = True
                await asyncio.sleep(0.02)
                return [Candidate(source=self.name, url="http://example.com/test.pdf")]

            async def fetch(self, candidate, ctx):
                return b"%PDF-1.4 fake pdf content that passes verify" * 50

        doc_id = db.add_query(DocumentQuery(doi="10.1234/cancel.1"))
        doc = db.get(doc_id)

        cancel = asyncio.Event()
        cancel.set()

        pipeline = Pipeline(cfg, db, cancel_event=cancel, workers=1)
        s1 = StubSource("src1")
        s2 = StubSource("src2")
        pipeline._source_instances = [s1, s2]

        await pipeline.run([doc])

        assert not s1.search_called
        assert not s2.search_called
        db.close()

    asyncio.run(_run())
