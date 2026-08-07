"""Webhook event notification engine."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from documentcrawler.db import Database
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)


class WebhookDispatcher:
    """Asynchronously dispatches webhook events to registered HTTP endpoints."""

    def __init__(self, db: Database, timeout_s: float = 10.0, max_retries: int = 3):
        self.db = db
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    async def dispatch(self, event: str, payload: dict[str, Any]) -> dict[str, int]:
        """Dispatch event payload to all matching registered webhooks with exponential backoff retries."""
        import asyncio

        webhooks = self.db.list_webhooks()
        if not webhooks:
            return {"sent": 0, "failed": 0}

        sent = 0
        failed = 0
        now_iso = datetime.now(UTC).isoformat()
        body = json.dumps({"event": event, "payload": payload, "timestamp": now_iso})

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            for hook in webhooks:
                events_pattern = hook.get("events", "*")
                if events_pattern != "*" and event not in [e.strip() for e in events_pattern.split(",")]:
                    continue

                headers = {"Content-Type": "application/json", "User-Agent": "OpenInfoLibrarian-Webhook/0.3.8"}
                secret = hook.get("secret")
                if secret:
                    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
                    headers["X-Webhook-Signature"] = f"sha256={sig}"

                success = False
                for attempt in range(1, self.max_retries + 1):
                    try:
                        res = await client.post(hook["url"], content=body, headers=headers)
                        if res.is_success:
                            sent += 1
                            success = True
                            break
                        log.warning("Webhook attempt %d/%d to %s returned status %d", attempt, self.max_retries, hook["url"], res.status_code)
                    except Exception as e:
                        log.warning("Webhook attempt %d/%d failed for %s: %s", attempt, self.max_retries, hook["url"], e)
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.01 * (2 ** (attempt - 1)))
                if not success:
                    failed += 1

        return {"sent": sent, "failed": failed}


def verify_webhook_signature(secret: str, body: str | bytes, signature_header: str) -> bool:
    """Verify an incoming HMAC-SHA256 webhook signature header (format: sha256=<hex_digest>)."""
    if not secret or not signature_header:
        return False
    sig_parts = signature_header.split("=", 1)
    if len(sig_parts) != 2 or sig_parts[0].lower() != "sha256":
        return False

    expected_sig = sig_parts[1].strip()
    payload_bytes = body.encode("utf-8") if isinstance(body, str) else body
    computed_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_sig, expected_sig)
