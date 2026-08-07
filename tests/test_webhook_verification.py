"""Unit tests for webhook HMAC signature verification."""

from __future__ import annotations

import hashlib
import hmac

from documentcrawler.webhooks import verify_webhook_signature


def test_verify_webhook_signature():
    secret = "my_secret_key"
    payload = '{"event": "document.done", "doc_id": 42}'
    computed_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    header = f"sha256={computed_sig}"

    assert verify_webhook_signature(secret, payload, header) is True
    assert verify_webhook_signature("wrong_secret", payload, header) is False
    assert verify_webhook_signature(secret, payload, "sha256=invalidhex") is False
    assert verify_webhook_signature(secret, payload, "") is False
