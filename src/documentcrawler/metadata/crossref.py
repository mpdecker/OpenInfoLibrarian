"""Crossref REST API helpers."""

from __future__ import annotations

from typing import Any

from documentcrawler.fetcher import Fetcher

_BASE = "https://api.crossref.org"


async def crossref_lookup(fetcher: Fetcher, doi: str, *, mailto: str | None = None) -> dict[str, Any] | None:
    params: dict[str, Any] = {}
    if mailto:
        params["mailto"] = mailto
    data = await fetcher.get_json(f"{_BASE}/works/{doi}", params=params)
    return data.get("message") if isinstance(data, dict) else None


async def crossref_search(
    fetcher: Fetcher,
    *,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
    rows: int = 5,
    mailto: str | None = None,
) -> dict[str, Any] | None:
    params: dict[str, Any] = {"rows": rows}
    if title:
        params["query.bibliographic"] = title
    if author:
        params["query.author"] = author
    if year:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"
    if mailto:
        params["mailto"] = mailto

    data = await fetcher.get_json(f"{_BASE}/works", params=params)
    items = (data or {}).get("message", {}).get("items") or []
    return items[0] if items else None
