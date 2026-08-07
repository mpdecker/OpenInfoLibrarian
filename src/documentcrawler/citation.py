"""BibTeX citation key generator utility."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.models import DocumentRow

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "over", "after",
}


def generate_citekey(doc: DocumentRow, existing_keys: set[str] | None = None) -> str:
    """Generate a clean, deterministic BibTeX citation key (e.g. Vaswani2017Attention).

    If existing_keys is provided and a collision occurs, appends a letter suffix (a, b, c...).
    """
    author_part = _extract_author_surname(doc.authors)
    year_part = str(doc.year) if doc.year else ""
    title_part = _extract_title_keyword(doc.title)

    base = f"{author_part}{year_part}{title_part}"
    if not base:
        base = f"doc_{doc.id}"

    # Clean characters (alphanumeric only)
    clean_base = re.sub(r"[^A-Za-z0-9]", "", base)
    if not clean_base:
        clean_base = f"doc_{doc.id}"

    if existing_keys is None:
        return clean_base

    candidate = clean_base
    if candidate not in existing_keys:
        existing_keys.add(candidate)
        return candidate

    # Resolve collision with suffix a, b, c...
    suffix_idx = 0
    while True:
        suffix = _idx_to_suffix(suffix_idx)
        key_with_suffix = f"{clean_base}{suffix}"
        if key_with_suffix not in existing_keys:
            existing_keys.add(key_with_suffix)
            return key_with_suffix
        suffix_idx += 1


def _extract_author_surname(authors: list[str]) -> str:
    if not authors:
        return ""
    first_author = authors[0].strip()
    if "," in first_author:
        surname = first_author.split(",")[0].strip()
    else:
        parts = first_author.split()
        surname = parts[-1] if parts else first_author
    surname_clean = re.sub(r"[^A-Za-z]", "", surname)
    return surname_clean.capitalize()


def _extract_title_keyword(title: str | None) -> str:
    if not title:
        return ""
    words = re.findall(r"[A-Za-z0-9]+", title)
    for word in words:
        if word.lower() not in _STOP_WORDS and len(word) >= 3:
            return word.capitalize()
    return words[0].capitalize() if words else ""


def _idx_to_suffix(idx: int) -> str:
    # 0 -> 'a', 1 -> 'b', ..., 25 -> 'z', 26 -> 'aa'
    result = []
    while True:
        result.append(chr(ord('a') + (idx % 26)))
        idx = idx // 26 - 1
        if idx < 0:
            break
    return "".join(reversed(result))


def generate_unique_citekeys(docs: list[DocumentRow]) -> dict[int, str]:
    """Generate collision-free BibTeX citekeys for a list of documents. Returns doc_id -> citekey map."""
    existing_keys: set[str] = set()
    out: dict[int, str] = {}
    for doc in docs:
        key = generate_citekey(doc, existing_keys=existing_keys)
        out[doc.id] = key
    return out
