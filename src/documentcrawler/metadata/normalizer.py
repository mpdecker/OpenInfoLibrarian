"""Automated metadata normalization engine for cleaning DOIs, author names, and titles."""

from __future__ import annotations

import re


def normalize_doi(doi: str | None) -> str | None:
    """Clean and normalize a DOI string by removing URL prefixes and whitespace."""
    if not doi:
        return None
    cleaned = doi.strip()
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^doi:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or None


def normalize_author_name(name: str) -> str:
    """Standardize author name into 'Last, First M.' format if possible."""
    name = re.sub(r"\s+", " ", name.strip())
    if not name:
        return ""
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return f"{parts[0]}, {parts[1]}" if len(parts) == 2 and parts[1] else parts[0]

    parts = name.split(" ")
    if len(parts) >= 2:
        last = parts[-1]
        first_middle = " ".join(parts[:-1])
        return f"{last}, {first_middle}"
    return name


def normalize_authors(authors: list[str] | None) -> list[str]:
    """Normalize a list of author name strings."""
    if not authors:
        return []
    result: list[str] = []
    for a in authors:
        norm = normalize_author_name(a)
        if norm and norm not in result:
            result.append(norm)
    return result


def normalize_title(title: str | None) -> str | None:
    """Normalize document title whitespace and clean surrounding quotes."""
    if not title:
        return None
    cleaned = re.sub(r"\s+", " ", title.strip())
    cleaned = cleaned.strip("\"'‘’“”")
    return cleaned.strip() or None
