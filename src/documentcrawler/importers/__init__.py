from collections.abc import Iterator
from pathlib import Path

from documentcrawler.importers.bibtex_importer import parse_bibtex
from documentcrawler.importers.csv_importer import parse_csv
from documentcrawler.importers.doi_list import parse_doi_list
from documentcrawler.importers.ris_importer import parse_ris
from documentcrawler.metadata.normalizer import (
    normalize_authors,
    normalize_doi,
    normalize_title,
)
from documentcrawler.models import DocumentQuery


def _normalize_query(q: DocumentQuery) -> DocumentQuery:
    return DocumentQuery(
        doi=normalize_doi(q.doi),
        title=normalize_title(q.title),
        authors=normalize_authors(q.authors),
        year=q.year,
        isbn=q.isbn,
        keywords=q.keywords,
        extra=q.extra,
        url=q.url,
    )


def parse_file(path: Path) -> Iterator[DocumentQuery]:
    """Detect the file type by extension and yield queries."""
    suffix = path.suffix.lower()
    if suffix in (".bib", ".bibtex"):
        raw_iter = parse_bibtex(path)
    elif suffix in (".ris",):
        raw_iter = parse_ris(path)
    elif suffix in (".csv", ".tsv"):
        raw_iter = parse_csv(path)
    elif suffix in (".txt", ".lst"):
        raw_iter = parse_doi_list(path)
    else:
        raise ValueError(f"Unsupported import format: {suffix} ({path.name})")

    for q in raw_iter:
        yield _normalize_query(q)


__all__ = ["parse_file", "parse_bibtex", "parse_csv", "parse_doi_list", "parse_ris"]
