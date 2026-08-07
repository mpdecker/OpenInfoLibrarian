"""Unit tests for citation key generation and BibTeX export."""

from __future__ import annotations

from datetime import UTC, datetime

from documentcrawler.citation import generate_citekey
from documentcrawler.exporters import export_bibtex
from documentcrawler.models import DocStatus, DocumentRow


def test_generate_citekey_basic():
    doc = DocumentRow(
        id=1,
        doi="10.1000/182",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish", "Shazeer, Noam"],
        year=2017,
        isbn=None,
        keywords=[],
        extra={},
        url=None,
        status=DocStatus.DONE,
        file_path=None,
        sha256=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    key = generate_citekey(doc)
    assert key == "Vaswani2017Attention"


def test_generate_citekey_collision_handling():
    doc1 = DocumentRow(
        id=1,
        doi="10.1000/1",
        title="Deep Residual Learning for Image Recognition",
        authors=["He, Kaiming", "Zhang, Xiangyu"],
        year=2016,
        isbn=None,
        keywords=[],
        extra={},
        url=None,
        status=DocStatus.DONE,
        file_path=None,
        sha256=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    doc2 = DocumentRow(
        id=2,
        doi="10.1000/2",
        title="Deep Reinforcement Learning",
        authors=["He, Kaiming"],
        year=2016,
        isbn=None,
        keywords=[],
        extra={},
        url=None,
        status=DocStatus.DONE,
        file_path=None,
        sha256=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    seen = set()
    k1 = generate_citekey(doc1, existing_keys=seen)
    k2 = generate_citekey(doc2, existing_keys=seen)

    assert k1 == "He2016Deep"
    assert k2 == "He2016Deepa"
    assert len(seen) == 2


def test_export_bibtex_unique_citekeys():
    doc1 = DocumentRow(
        id=10,
        doi="10.1000/a",
        title="Graph Neural Networks",
        authors=["Scarselli, Franco"],
        year=2009,
        isbn=None,
        keywords=[],
        extra={},
        url=None,
        status=DocStatus.DONE,
        file_path=None,
        sha256=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    doc2 = DocumentRow(
        id=11,
        doi="10.1000/b",
        title="Graph Convolutional Networks",
        authors=["Scarselli, Franco"],
        year=2009,
        isbn=None,
        keywords=[],
        extra={},
        url=None,
        status=DocStatus.DONE,
        file_path=None,
        sha256=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    bib = export_bibtex([doc1, doc2])
    assert "@article{Scarselli2009Graph," in bib
    assert "@article{Scarselli2009Grapha," in bib
