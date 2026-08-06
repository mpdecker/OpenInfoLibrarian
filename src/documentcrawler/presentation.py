"""GUI-facing normalization helpers for cross-source search results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

_SOURCE_LABELS = {
    "annas_archive": "Anna's Archive",
    "arxiv": "arXiv",
    "core": "CORE (OA)",
    "crossref": "Crossref",
    "doi_org": "DOI.org",
    "elsevier": "Elsevier",
    "europeana": "Europeana",
    "ieee": "IEEE Xplore",
    "jstor": "JSTOR",
    "libgen": "LibGen",
    "library_of_congress": "Library of Congress",
    "openalex": "OpenAlex",
    "openlibrary": "Open Library",
    "scihub": "Sci-Hub",
    "semantic_scholar": "Semantic Scholar",
    "springer": "Springer",
    "uk_national_archives": "UK National Archives",
    "wiley": "Wiley",
    "zlibrary": "Z-Library",
}


@dataclass(slots=True)
class NormalizedSearchHit:
    title: str
    authors: str
    year: str
    venue: str
    identifier: str
    availability: str
    sources: str
    preview_text: str


@dataclass(slots=True)
class NormalizedDocumentRow:
    title: str
    authors: str
    year: str
    identifier: str
    status: str
    file: str
    page: str


class SearchHitLike(Protocol):
    source: str
    title: str | None
    authors: list[str]
    year: int | None
    doi: str | None
    isbn: str | None
    abstract: str | None
    url: str | None
    pdf_url: str | None
    container: str | None
    extra: dict[str, object]

    @property
    def author_str(self) -> str: ...


class DocumentRowLike(Protocol):
    title: str | None
    authors: list[str]
    year: int | None
    doi: str | None
    isbn: str | None
    url: str | None
    file_path: str | None
    status: object


def normalize_search_hit(hit: SearchHitLike) -> NormalizedSearchHit:
    title = (hit.title or "").strip() or _fallback_title(hit)
    authors = hit.author_str or "Unknown author"
    year = str(hit.year) if hit.year else ""
    venue = _venue(hit)
    identifier = _primary_identifier(hit)
    availability = _availability(hit)
    sources = _sources(hit)

    preview_lines = [title]
    preview_lines.append(f"Authors: {authors}")
    published = " | ".join(part for part in (year, venue) if part)
    if published:
        preview_lines.append(f"Published: {published}")
    if hit.doi:
        preview_lines.append(f"DOI: {hit.doi}")
    if hit.isbn:
        preview_lines.append(f"ISBN: {hit.isbn}")
    if (arxiv_id := _arxiv_id(hit)):
        preview_lines.append(f"arXiv: {arxiv_id}")
    if (source_record := _source_record_identifier(hit)):
        preview_lines.append(f"Source record: {source_record}")
    if (format_line := _format_line(hit)):
        preview_lines.append(f"Format: {format_line}")
    preview_lines.append(f"Availability: {availability}")
    preview_lines.append(f"Sources: {sources}")
    if hit.url:
        preview_lines.append(f"Landing page: {hit.url}")
    if hit.pdf_url:
        preview_lines.append(f"PDF: {hit.pdf_url}")
    if hit.abstract:
        abstract = hit.abstract
        if len(abstract) > 1500:
            abstract = abstract[:1500].rstrip() + " ..."
        preview_lines.extend(("", abstract))

    return NormalizedSearchHit(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        identifier=identifier,
        availability=availability,
        sources=sources,
        preview_text="\n".join(preview_lines).strip(),
    )


def normalize_document_row(row: DocumentRowLike) -> NormalizedDocumentRow:
    title = (row.title or "").strip() or _fallback_document_title(row)
    authors = _author_str(row.authors) or "Unknown author"
    year = str(row.year) if row.year else ""
    identifier = _document_identifier(row)
    status = getattr(row.status, "value", str(row.status or ""))
    return NormalizedDocumentRow(
        title=title,
        authors=authors,
        year=year,
        identifier=identifier,
        status=status,
        file=row.file_path or "",
        page=row.url or "",
    )


def preferred_queue_url(hit: SearchHitLike) -> str | None:
    return hit.url or hit.pdf_url


def copyable_document_url(row: DocumentRowLike) -> str | None:
    return row.url or None


def _fallback_title(hit: SearchHitLike) -> str:
    if hit.doi:
        return f"DOI {hit.doi}"
    if hit.isbn:
        return f"ISBN {hit.isbn}"
    if arxiv_id := _arxiv_id(hit):
        return f"arXiv {arxiv_id}"
    if source_record := _source_record_identifier(hit):
        return source_record
    return "Untitled result"


def _fallback_document_title(row: DocumentRowLike) -> str:
    if row.doi:
        return f"DOI {row.doi}"
    if row.isbn:
        return f"ISBN {row.isbn}"
    if row.url:
        parsed = urlparse(row.url)
        return parsed.netloc or row.url
    return "Untitled document"


def _venue(hit: SearchHitLike) -> str:
    if hit.container:
        return hit.container
    publisher = hit.extra.get("publisher")
    if isinstance(publisher, str) and publisher.strip():
        return publisher.strip()
    work_type = hit.extra.get("type")
    if isinstance(work_type, str) and work_type.strip():
        return work_type.replace("-", " ").title()
    return ""


def _primary_identifier(hit: SearchHitLike) -> str:
    if hit.doi:
        return f"DOI {hit.doi}"
    if hit.isbn:
        return f"ISBN {hit.isbn}"
    if arxiv_id := _arxiv_id(hit):
        return f"arXiv {arxiv_id}"
    if source_record := _source_record_identifier(hit):
        return source_record
    if hit.url:
        parsed = urlparse(hit.url)
        host = parsed.netloc or hit.url
        path = parsed.path.rstrip("/")
        if path:
            return f"{host}{path}"
        return host
    return "No identifier"


def _availability(hit: SearchHitLike) -> str:
    if hit.pdf_url and hit.url:
        return "PDF + page"
    if hit.pdf_url:
        return "PDF"
    if hit.url:
        return "Page"
    return "Metadata only"


def _sources(hit: SearchHitLike) -> str:
    contributors = hit.extra.get("contributors")
    names = _ordered_sources(hit.source, contributors)
    return " + ".join(names)


def _ordered_sources(primary: str, contributors: object) -> list[str]:
    ordered = [_source_label(primary)]
    extras: list[str] = []
    if isinstance(contributors, Iterable) and not isinstance(contributors, (str, bytes, dict)):
        for name in contributors:
            if not isinstance(name, str):
                continue
            label = _source_label(name)
            if label not in ordered and label not in extras:
                extras.append(label)
    return ordered + sorted(extras)


def _source_label(name: str) -> str:
    return _SOURCE_LABELS.get(name, name.replace("_", " ").title())


def _author_str(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) <= 3:
        return "; ".join(authors)
    return "; ".join(authors[:3]) + f"; +{len(authors) - 3}"


def _arxiv_id(hit: SearchHitLike) -> str | None:
    arxiv_id = hit.extra.get("arxiv_id")
    if isinstance(arxiv_id, str) and arxiv_id.strip():
        return arxiv_id.strip()
    for value in (hit.pdf_url, hit.url):
        if not value:
            continue
        parsed = urlparse(value)
        path = parsed.path.strip("/")
        if parsed.netloc.endswith("arxiv.org") and path.startswith(("abs/", "pdf/")):
            ident = path.split("/", 1)[1]
            if ident.endswith(".pdf"):
                ident = ident[:-4]
            if ident:
                return ident
    return None


def _source_record_identifier(hit: SearchHitLike) -> str | None:
    if hit.source == "annas_archive" and isinstance(hit.extra.get("md5"), str) and hit.extra["md5"]:
        return f"Anna's Archive MD5 {hit.extra['md5']}"
    if hit.source == "libgen" and isinstance(hit.extra.get("md5"), str) and hit.extra["md5"]:
        return f"LibGen MD5 {hit.extra['md5']}"
    if hit.source == "zlibrary" and isinstance(hit.extra.get("zlib_id"), str) and hit.extra["zlib_id"]:
        return f"Z-Library ID {hit.extra['zlib_id']}"
    if hit.source == "openlibrary" and isinstance(hit.extra.get("ia_id"), str) and hit.extra["ia_id"]:
        return f"Internet Archive {hit.extra['ia_id']}"
    return None


def _format_line(hit: SearchHitLike) -> str:
    parts: list[str] = []
    ext = hit.extra.get("ext")
    if isinstance(ext, str) and ext.strip():
        parts.append(ext.strip().upper())
    language = hit.extra.get("language")
    if isinstance(language, str) and language.strip():
        parts.append(language.strip())
    filesize = hit.extra.get("filesize")
    if isinstance(filesize, str) and filesize.strip():
        parts.append(filesize.strip())
    return " | ".join(parts)


def _document_identifier(row: DocumentRowLike) -> str:
    if row.doi:
        return f"DOI {row.doi}"
    if row.isbn:
        return f"ISBN {row.isbn}"
    if row.url:
        parsed = urlparse(row.url)
        host = parsed.netloc or row.url
        path = parsed.path.rstrip("/")
        if path:
            return f"{host}{path}"
        return host
    return "No identifier"
