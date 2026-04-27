from collections.abc import Iterator
from pathlib import Path

from documentcrawler.importers.bibtex_importer import parse_bibtex
from documentcrawler.importers.csv_importer import parse_csv
from documentcrawler.importers.doi_list import parse_doi_list
from documentcrawler.importers.ris_importer import parse_ris
from documentcrawler.models import DocumentQuery


def parse_file(path: Path) -> Iterator[DocumentQuery]:
    """Detect the file type by extension and yield queries."""
    suffix = path.suffix.lower()
    if suffix in (".bib", ".bibtex"):
        return parse_bibtex(path)
    if suffix in (".ris",):
        return parse_ris(path)
    if suffix in (".csv", ".tsv"):
        return parse_csv(path)
    if suffix in (".txt", ".lst"):
        return parse_doi_list(path)
    raise ValueError(f"Unsupported import format: {suffix} ({path.name})")


__all__ = ["parse_file", "parse_bibtex", "parse_csv", "parse_doi_list", "parse_ris"]
