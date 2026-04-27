"""Unpaywall API helpers."""

from __future__ import annotations

from typing import Any

from documentcrawler.fetcher import Fetcher

_BASE = "https://api.unpaywall.org/v2"


async def unpaywall_lookup(fetcher: Fetcher, doi: str, email: str) -> dict[str, Any] | None:
    if not email:
        return None
    url = f"{_BASE}/{doi}"
    try:
        return await fetcher.get_json(url, params={"email": email})
    except Exception:
        return None
