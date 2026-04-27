"""Plain DOI list (one per line, # comments allowed)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from documentcrawler.models import DocumentQuery
from documentcrawler.utils.sanitize import normalize_doi


def parse_doi_list(path: Path) -> Iterator[DocumentQuery]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        doi = normalize_doi(line)
        if doi:
            yield DocumentQuery(doi=doi)
