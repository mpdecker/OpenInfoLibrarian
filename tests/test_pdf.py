"""Unit tests for PDF structure inspection utility."""

from __future__ import annotations

import pytest

from documentcrawler.pdf import inspect_pdf
from documentcrawler.storage.writer import InvalidPDFError


def test_inspect_pdf_valid(tmp_path):
    pdf_path = tmp_path / "valid_sample.pdf"
    # Create minimal valid PDF content
    pdf_content = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Title (Test Paper Title) /Author (Jane Doe) >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )
    pdf_path.write_bytes(pdf_content)

    res = inspect_pdf(pdf_path)
    assert res["is_valid_pdf"] is True
    assert res["pdf_version"] == "1.4"
    assert res["page_count"] == 1
    assert res["metadata"].get("title") == "Test Paper Title"
    assert res["metadata"].get("author") == "Jane Doe"


def test_inspect_pdf_invalid(tmp_path):
    invalid_path = tmp_path / "invalid.pdf"
    invalid_path.write_bytes(b"NOT A PDF FILE CONTENT AT ALL")

    with pytest.raises(InvalidPDFError):
        inspect_pdf(invalid_path)
