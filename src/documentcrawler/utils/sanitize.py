"""String sanitization helpers for filenames and DOIs."""

from __future__ import annotations

import re
import unicodedata

from slugify import slugify

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arXiv:)?(\d{4}\.\d{4,6})(v\d+)?", re.IGNORECASE)
_ISBN_RE = re.compile(r"\b(?:97[89][- ]?)?\d[\d\- ]{8,16}[\dxX]\b")


def normalize_doi(value: str | None) -> str | None:
    """Strip common prefixes / URL wrappers from a DOI string."""
    if not value:
        return None
    s = value.strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = s.removeprefix("doi:").removeprefix("DOI:").strip()
    m = _DOI_RE.search(s)
    return m.group(0).rstrip(".,)") if m else None


def extract_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    m = _ARXIV_RE.search(value)
    return m.group(1) if m else None


def normalize_isbn(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9xX]", "", value)
    return digits or None


def title_slug(title: str | None, max_len: int = 60) -> str:
    if not title:
        return "untitled"
    slug = slugify(title, max_length=max_len, lowercase=True)
    return slug or "untitled"


def safe_filename_part(value: str | None, max_len: int = 80) -> str:
    if not value:
        return "unknown"
    norm = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^A-Za-z0-9._-]+", "_", norm).strip("_")
    return (norm[:max_len] or "unknown")
