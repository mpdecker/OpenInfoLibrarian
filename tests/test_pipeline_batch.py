"""Unit tests for Pipeline.process_batch concurrent processing."""

import pytest

from documentcrawler.config import Config
from documentcrawler.db import Database
from documentcrawler.models import DocumentQuery
from documentcrawler.pipeline import Pipeline


@pytest.mark.asyncio
async def test_pipeline_process_batch_concurrent(tmp_path):
    db_path = tmp_path / "batch_test.db"
    db = Database(db_path)

    id1 = db.add_query(DocumentQuery(doi="10.1000/b1", title="Batch Paper 1"))
    id2 = db.add_query(DocumentQuery(doi="10.1000/b2", title="Batch Paper 2"))

    docs = [db.get(id1), db.get(id2)]
    docs = [d for d in docs if d is not None]

    cfg = Config()
    pipeline = Pipeline(cfg, db)

    results = await pipeline.process_batch(docs, max_concurrency=2)
    assert len(results) == 2
    assert all(r.id in (id1, id2) for r in results)
    db.close()
