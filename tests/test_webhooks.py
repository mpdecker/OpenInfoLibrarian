"""Unit tests for Webhooks event notification dispatcher."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from documentcrawler.db import Database
from documentcrawler.webhooks import WebhookDispatcher


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_webhooks.db"
    database = Database(db_path)
    yield database
    database.close()


def test_webhook_crud(db: Database):
    hooks = db.list_webhooks()
    assert len(hooks) == 0

    wid1 = db.add_webhook("http://localhost:8000/callback", events="*", secret="secret123")
    assert wid1 > 0

    wid2 = db.add_webhook("http://localhost:8000/doc_events", events="doc_start,doc_done")
    assert wid2 > 0

    hooks = db.list_webhooks()
    assert len(hooks) == 2
    assert hooks[0]["url"] == "http://localhost:8000/callback"
    assert hooks[0]["secret"] == "secret123"

    ok = db.delete_webhook(wid1)
    assert ok is True
    assert len(db.list_webhooks()) == 1


@pytest.mark.asyncio
async def test_webhook_dispatcher(db: Database):
    db.add_webhook("http://example.com/webhook", events="doc_done", secret="mysecret")
    dispatcher = WebhookDispatcher(db)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.is_success = True
        result = await dispatcher.dispatch("doc_done", {"id": 42, "title": "Test Paper"})

        assert result["sent"] == 1
        assert result["failed"] == 0
        assert mock_post.called
        kwargs = mock_post.call_args.kwargs
        headers = kwargs.get("headers", {})
        assert "X-Webhook-Signature" in headers
        assert headers["X-Webhook-Signature"].startswith("sha256=")

        payload_sent = json.loads(kwargs.get("content", "{}"))
        assert payload_sent["event"] == "doc_done"
        assert payload_sent["payload"]["id"] == 42
