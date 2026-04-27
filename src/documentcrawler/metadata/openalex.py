"""OpenAlex API helpers."""

from __future__ import annotations

from typing import Any

from documentcrawler.fetcher import Fetcher

_BASE = "https://api.openalex.org"


async def openalex_lookup(fetcher: Fetcher, doi: str) -> dict[str, Any] | None:
    url = f"{_BASE}/works/doi:{doi}"
    try:
        return await fetcher.get_json(url)
    except Exception:
        return None
