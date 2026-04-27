"""Filename / folder template rendering."""

from __future__ import annotations

from pathlib import Path

from documentcrawler.models import DocumentRow
from documentcrawler.utils.sanitize import safe_filename_part, title_slug


def _first_author_last(authors: list[str]) -> str:
    if not authors:
        return "unknown"
    first = authors[0].strip()
    if "," in first:
        return safe_filename_part(first.split(",", 1)[0].strip())
    parts = first.split()
    return safe_filename_part(parts[-1] if parts else first)


def _render(template: str, doc: DocumentRow, ext: str) -> str:
    last = _first_author_last(doc.authors)
    initial = (last[:1] or "U").upper()
    fields = {
        "first_author_last": last,
        "first_author_initial": initial,
        "year": doc.year if doc.year else "n.d.",
        "title_slug": title_slug(doc.title),
        "doi_slug": safe_filename_part(doc.doi.replace("/", "_")) if doc.doi else "no-doi",
        "ext": ext.lstrip("."),
        "id": doc.id,
    }
    try:
        return template.format(**fields)
    except KeyError as e:
        return f"unknown_{doc.id}.{ext.lstrip('.')}".replace(" ", "_") + f"#bad-key-{e.args[0]}"


def render_destination(
    download_dir: Path,
    doc: DocumentRow,
    *,
    filename_template: str,
    folder_template: str | None,
    ext: str = "pdf",
) -> Path:
    """Compute the absolute destination Path for a document."""
    folder = download_dir
    if folder_template:
        sub = _render(folder_template, doc, ext=ext)
        folder = folder / sub
    name = _render(filename_template, doc, ext=ext)
    return folder / name
