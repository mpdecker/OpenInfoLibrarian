"""Unit tests for Prometheus operational metrics generator."""

from __future__ import annotations

from documentcrawler.db import Database
from documentcrawler.metrics import generate_prometheus_metrics
from documentcrawler.models import DocumentQuery


def test_generate_prometheus_metrics(tmp_path):
    db_path = tmp_path / "metrics_test.db"
    db = Database(db_path)

    db.add_query(DocumentQuery(title="Test Paper 1"))
    db.add_webhook("https://example.com/webhook")

    metrics_text = generate_prometheus_metrics(db)

    assert "document_crawler_documents_total" in metrics_text
    assert 'status="pending"' in metrics_text
    assert "document_crawler_webhooks_registered 1" in metrics_text

    db.close()
