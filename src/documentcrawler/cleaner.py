"""Automated metadata sanitization and cleaning module."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.models import DocumentRow

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SURNAME_GIVEN_RE = re.compile(r"^([^,]+),\s*(.+)$")


def clean_title(title: str | None) -> str | None:
    """Sanitize title string by unescaping HTML entities, removing HTML tags, and trimming excess whitespace."""
    if not title:
        return None
    unescaped = html.unescape(title)
    stripped = _HTML_TAG_RE.sub("", unescaped)
    cleaned = re.sub(r"\s+", " ", stripped).strip()
    return cleaned if cleaned else None


def clean_author_name(author: str) -> str:
    """Sanitize and format individual author name into clean 'Surname, Given' or 'Given Surname' format."""
    if not author:
        return ""
    unescaped = html.unescape(author)
    stripped = _HTML_TAG_RE.sub("", unescaped).strip()
    cleaned = re.sub(r"\s+", " ", stripped)

    m = _SURNAME_GIVEN_RE.match(cleaned)
    if m:
        surname = m.group(1).strip().title()
        given = m.group(2).strip().title()
        return f"{surname}, {given}"

    parts = cleaned.split()
    if len(parts) >= 2:
        surname = parts[-1].title()
        given = " ".join(parts[:-1]).title()
        return f"{surname}, {given}"
    return cleaned.title()


def clean_authors(authors: list[str]) -> list[str]:
    """Sanitize list of author names."""
    out: list[str] = []
    seen: set[str] = set()
    for a in authors:
        ca = clean_author_name(a)
        if ca and ca.lower() not in seen:
            out.append(ca)
            seen.add(ca.lower())
    return out


def clean_document_metadata(doc: DocumentRow) -> dict[str, Any]:
    """Clean all metadata fields of a DocumentRow and return updated field dictionary."""
    cleaned_title = clean_title(doc.title)
    cleaned_authors = clean_authors(doc.authors)

    cleaned_keywords = [
        re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub("", kw))).strip()
        for kw in doc.keywords
        if kw.strip()
    ]
    cleaned_keywords = list(dict.fromkeys(k for k in cleaned_keywords if k))

    return {
        "title": cleaned_title,
        "authors": cleaned_authors,
        "keywords": cleaned_keywords,
    }
