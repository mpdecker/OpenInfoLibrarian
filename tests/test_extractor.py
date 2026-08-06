"""Unit tests for PDF text extractor module."""

from documentcrawler.storage.extractor import extract_pdf_text


def test_extract_pdf_text_from_mock_pdf(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    # Create minimal valid PDF structure with BT/ET text operator
    pdf_content = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length 55 >> stream\n"
        b"BT /F1 12 Tf (Attention Is All You Need) Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    pdf_path.write_bytes(pdf_content)

    text = extract_pdf_text(pdf_path)
    assert "Attention Is All You Need" in text


def test_extract_pdf_text_nonexistent():
    assert extract_pdf_text("nonexistent.pdf") == ""
