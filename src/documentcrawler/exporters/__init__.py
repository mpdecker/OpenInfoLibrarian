"""Exporters for dumping DocumentRow instances to BibTeX, RIS, CSV, and JSONL."""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Sequence
from typing import Any

from documentcrawler.models import DocumentRow


def _first_author_surname(authors: list[str]) -> str:
    if not authors:
        return "Unknown"
    name = authors[0].strip()
    if "," in name:
        return name.split(",")[0].strip().capitalize()
    parts = name.split()
    return parts[-1].capitalize() if parts else "Unknown"


def _make_cite_key(doc: DocumentRow, template: str | None = None) -> str:
    author = _first_author_surname(doc.authors)
    year = str(doc.year) if doc.year else "nd"
    if doc.title:
        words = [w for w in re.sub(r"[^a-zA-Z0-9\s]", "", doc.title).split() if len(w) > 2]
        first_word = words[0].capitalize() if words else "Doc"
    else:
        first_word = "Doc"

    if template:
        key = template.format(
            author=author,
            year=year,
            title_word=first_word,
            id=doc.id,
        )
        return re.sub(r"[^a-zA-Z0-9_-]", "", key)
    return f"{author}{year}{first_word}"


def export_bibtex(docs: Sequence[DocumentRow], citekey_template: str | None = None) -> str:
    """Format DocumentRow instances as a BibTeX bibliography string with unique citekeys."""
    from documentcrawler.citation import generate_citekey

    entries: list[str] = []
    seen_keys: set[str] = set()

    for doc in docs:
        if citekey_template:
            key = _make_cite_key(doc, template=citekey_template)
            while key in seen_keys:
                key = f"{key}_dup"
            seen_keys.add(key)
        else:
            key = generate_citekey(doc, existing_keys=seen_keys)

        entry_type = "article" if doc.doi else "book" if doc.isbn else "misc"
        lines = [f"@{entry_type}{{{key},"]
        if doc.title:
            lines.append(f'  title = {{{doc.title}}},')
        if doc.authors:
            lines.append(f'  author = {{{" and ".join(doc.authors)}}},')
        if doc.year:
            lines.append(f'  year = {{{doc.year}}},')
        if doc.doi:
            lines.append(f'  doi = {{{doc.doi}}},')
        if doc.isbn:
            lines.append(f'  isbn = {{{doc.isbn}}},')
        if doc.keywords:
            lines.append(f'  keywords = {{{", ".join(doc.keywords)}}},')
        if doc.url:
            lines.append(f'  url = {{{doc.url}}},')
        container = doc.extra.get("container") or doc.extra.get("journal")
        if container:
            lines.append(f'  journal = {{{container}}},')
        lines.append("}")
        entries.append("\n".join(lines))
    return "\n\n".join(entries)


def export_ris(docs: Sequence[DocumentRow]) -> str:
    """Format DocumentRow instances as a RIS bibliography string."""
    entries: list[str] = []
    for doc in docs:
        lines: list[str] = []
        ty = "JOUR" if doc.doi else "BOOK" if doc.isbn else "GEN"
        lines.append(f"TY  - {ty}")
        if doc.title:
            lines.append(f"TI  - {doc.title}")
        for author in doc.authors:
            lines.append(f"AU  - {author}")
        if doc.year:
            lines.append(f"PY  - {doc.year}")
        if doc.doi:
            lines.append(f"DO  - {doc.doi}")
        if doc.isbn:
            lines.append(f"SN  - {doc.isbn}")
        if doc.url:
            lines.append(f"UR  - {doc.url}")
        if doc.keywords:
            for kw in doc.keywords:
                lines.append(f"KW  - {kw}")
        container = doc.extra.get("container") or doc.extra.get("journal")
        if container:
            lines.append(f"T2  - {container}")
        lines.append("ER  - ")
        entries.append("\n".join(lines))
    return "\n\n".join(entries)


def export_csv(docs: Sequence[DocumentRow]) -> str:
    """Format DocumentRow instances as CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "doi", "title", "authors", "year", "isbn",
        "keywords", "url", "status", "file_path", "sha256"
    ])
    for doc in docs:
        writer.writerow([
            doc.id,
            doc.doi or "",
            doc.title or "",
            "; ".join(doc.authors),
            doc.year or "",
            doc.isbn or "",
            "; ".join(doc.keywords),
            doc.url or "",
            doc.status.value,
            doc.file_path or "",
            doc.sha256 or "",
        ])
    return output.getvalue()


def export_jsonl(docs: Sequence[DocumentRow]) -> str:
    """Format DocumentRow instances as JSON Lines string."""
    lines: list[str] = []
    for doc in docs:
        payload: dict[str, Any] = {
            "id": doc.id,
            "doi": doc.doi,
            "title": doc.title,
            "authors": doc.authors,
            "year": doc.year,
            "isbn": doc.isbn,
            "keywords": doc.keywords,
            "url": doc.url,
            "status": doc.status.value,
            "file_path": doc.file_path,
            "sha256": doc.sha256,
            "created_at": doc.created_at.isoformat(),
            "updated_at": doc.updated_at.isoformat(),
        }
        lines.append(json.dumps(payload))
    return "\n".join(lines)


__all__ = ["export_bibtex", "export_ris", "export_csv", "export_jsonl"]
