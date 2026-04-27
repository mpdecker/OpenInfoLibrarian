"""BibTeX importer."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import bibtexparser

from documentcrawler.models import DocumentQuery
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn


def parse_bibtex(path: Path) -> Iterator[DocumentQuery]:
    text = Path(path).read_text(encoding="utf-8")
    db = bibtexparser.loads(text)
    for entry in db.entries:
        q = _entry_to_query(entry)
        if not q.is_empty():
            yield q


def _entry_to_query(entry: dict[str, str]) -> DocumentQuery:
    authors_raw = entry.get("author") or entry.get("editor") or ""
    authors = [a.strip() for a in re.split(r"\s+and\s+", authors_raw) if a.strip()]

    year_raw = entry.get("year") or ""
    m = re.search(r"\d{4}", year_raw)
    year = int(m.group(0)) if m else None

    return DocumentQuery(
        doi=normalize_doi(entry.get("doi")),
        title=_strip_braces(entry.get("title")),
        authors=[_strip_braces(a) or "" for a in authors] if authors else [],
        year=year,
        isbn=normalize_isbn(entry.get("isbn")),
        keywords=[k.strip() for k in re.split(r"[;,]", entry.get("keywords") or "") if k.strip()],
        extra={
            key: value
            for key, value in {
                "journal": _strip_braces(entry.get("journal")),
                "booktitle": _strip_braces(entry.get("booktitle")),
                "editor": _strip_braces(entry.get("editor")),
                "publisher": _strip_braces(entry.get("publisher")),
            }.items()
            if value
        },
        url=entry.get("url"),
    )


def _strip_braces(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("{", "").replace("}", "").strip() or None
