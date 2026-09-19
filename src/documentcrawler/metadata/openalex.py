"""OpenAlex API helpers."""

from __future__ import annotations

from typing import Any

from documentcrawler.fetcher import Fetcher

_BASE = "https://api.openalex.org"


async def openalex_lookup(
    fetcher: Fetcher, doi: str, *, mailto: str | None = None
) -> dict[str, Any] | None:
    # mailto routes the request into OpenAlex's polite pool — without it,
    # datacenter egress IPs (e.g. serverless functions) get 403-throttled
    # quickly.
    params: dict[str, str] = {"mailto": mailto} if mailto else {}
    url = f"{_BASE}/works/doi:{doi}"
    try:
        return await fetcher.get_json(url, params=params or None)
    except Exception:
        return None
