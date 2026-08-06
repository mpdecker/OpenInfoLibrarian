"""Unit tests for bibliography exporters (BibTeX, RIS, CSV, JSONL)."""

from datetime import UTC, datetime

from documentcrawler.exporters import (
    export_bibtex,
    export_csv,
    export_jsonl,
    export_ris,
)
from documentcrawler.models import DocStatus, DocumentRow


def _sample_doc() -> DocumentRow:
    now = datetime.now(UTC)
    return DocumentRow(
        id=1,
        doi="10.1038/s41586-020-2649-2",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish", "Shazeer, Noam", "Parmar, Niki"],
        year=2017,
        isbn="978-0123456789",
        keywords=["transformer", "attention", "deep learning"],
        extra={"journal": "Advances in Neural Information Processing Systems"},
        url="https://arxiv.org/abs/1706.03762",
        status=DocStatus.DONE,
        file_path="/downloads/Vaswani2017Attention.pdf",
        sha256="abc123def456",
        created_at=now,
        updated_at=now,
    )


def test_export_bibtex():
    doc = _sample_doc()
    out = export_bibtex([doc])
    assert "@article{Vaswani2017Attention," in out
    assert "title = {Attention Is All You Need}" in out
    assert "doi = {10.1038/s41586-020-2649-2}" in out
    assert "author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki}" in out


def test_export_ris():
    doc = _sample_doc()
    out = export_ris([doc])
    assert "TY  - JOUR" in out
    assert "TI  - Attention Is All You Need" in out
    assert "AU  - Vaswani, Ashish" in out
    assert "PY  - 2017" in out
    assert "ER  - " in out


def test_export_csv():
    doc = _sample_doc()
    out = export_csv([doc])
    assert "id,doi,title,authors,year" in out
    assert "Attention Is All You Need" in out
    assert "10.1038/s41586-020-2649-2" in out


def test_export_jsonl():
    doc = _sample_doc()
    out = export_jsonl([doc])
    assert '"id": 1' in out
    assert '"doi": "10.1038/s41586-020-2649-2"' in out
