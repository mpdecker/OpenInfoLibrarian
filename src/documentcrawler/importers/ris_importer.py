"""RIS importer."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import rispy

from documentcrawler.models import DocumentQuery
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn


def parse_ris(path: Path) -> Iterator[DocumentQuery]:
    with Path(path).open(encoding="utf-8") as f:
        try:
            entries = rispy.load(f)
        except Exception:
            return
    for entry in entries:
        q = _entry_to_query(entry)
        if not q.is_empty():
            yield q


def _entry_to_query(entry: dict[str, object]) -> DocumentQuery:
    authors = entry.get("authors") or entry.get("first_authors") or []
    if isinstance(authors, str):
        authors = [authors]
    year_raw = str(entry.get("year") or entry.get("publication_year") or "")
    m = re.search(r"\d{4}", year_raw)
    year = int(m.group(0)) if m else None
    return DocumentQuery(
        doi=normalize_doi(entry.get("doi")),  # type: ignore[arg-type]
        title=str(entry.get("title") or entry.get("primary_title") or "") or None,
        authors=[str(a).strip() for a in authors if str(a).strip()],
        year=year,
        isbn=normalize_isbn(entry.get("isbn")),  # type: ignore[arg-type]
        keywords=[str(k).strip() for k in (entry.get("keywords") or []) if str(k).strip()],
        extra={
            key: value
            for key, value in {
                "journal": str(
                    entry.get("journal_name")
                    or entry.get("secondary_title")
                    or entry.get("periodical_name")
                    or ""
                ).strip()
                or None,
                "editor": "; ".join(
                    str(e).strip() for e in (entry.get("editors") or []) if str(e).strip()
                )
                or None,
                "publisher": str(entry.get("publisher") or "").strip() or None,
            }.items()
            if value
        },
        url=str(entry.get("url") or "") or None,
    )
