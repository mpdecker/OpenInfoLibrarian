"""Read DocumentQuery rows from CSV/TSV. Lenient column matching."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from pathlib import Path

from documentcrawler.models import DocumentQuery
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

_AUTHOR_SEP = re.compile(r"\s*(?:;|\band\b|&)\s*", re.IGNORECASE)
_KEYWORD_SEP = re.compile(r"\s*[;,]\s*")


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", h.lower())


_HEADER_ALIASES = {
    "doi": {"doi"},
    "title": {"title", "papertitle"},
    "authors": {"authors", "author", "authorslist"},
    "year": {"year", "publishedyear", "pubyear"},
    "isbn": {"isbn", "isbn10", "isbn13"},
    "keywords": {"keywords", "tags"},
    "url": {"url", "link"},
    "journal": {"journal", "journaltitle", "periodical", "venue"},
    "booktitle": {"booktitle", "book", "proceedings"},
    "editor": {"editor", "editors"},
    "publisher": {"publisher"},
}


def _map_headers(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for h in headers:
        norm = _norm_header(h)
        for canonical, aliases in _HEADER_ALIASES.items():
            if norm in aliases:
                mapping[canonical] = h
                break
    return mapping


def parse_csv(path: Path) -> Iterator[DocumentQuery]:
    text = Path(path).read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        return
    mapping = _map_headers(reader.fieldnames)
    for row in reader:
        q = _row_to_query(row, mapping)
        if not q.is_empty():
            yield q


def _row_to_query(row: dict[str, str], mapping: dict[str, str]) -> DocumentQuery:
    def get(key: str) -> str | None:
        col = mapping.get(key)
        if not col:
            return None
        val = (row.get(col) or "").strip()
        return val or None

    authors_raw = get("authors") or ""
    authors = [a.strip() for a in _AUTHOR_SEP.split(authors_raw) if a.strip()]
    keywords_raw = get("keywords") or ""
    keywords = [k.strip() for k in _KEYWORD_SEP.split(keywords_raw) if k.strip()]

    year_raw = get("year")
    year: int | None = None
    if year_raw:
        m = re.search(r"\d{4}", year_raw)
        if m:
            year = int(m.group(0))

    return DocumentQuery(
        doi=normalize_doi(get("doi")),
        title=get("title"),
        authors=authors,
        year=year,
        isbn=normalize_isbn(get("isbn")),
        keywords=keywords,
        extra={
            key: value
            for key, value in {
                "journal": get("journal"),
                "booktitle": get("booktitle"),
                "editor": get("editor"),
                "publisher": get("publisher"),
            }.items()
            if value
        },
        url=get("url"),
    )
