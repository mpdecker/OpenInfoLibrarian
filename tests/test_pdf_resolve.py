"""Tests for HTML → PDF URL extraction."""

from documentcrawler.fetcher.pdf_resolve import (
    is_probably_html,
    pdf_urls_from_html,
    prioritize_pdf_urls,
)


def test_iframe_and_pdf_anchor():
    html = """
    <html><body>
    <iframe id="pdf" src="/tree/paper.pdf"></iframe>
    <a href="https://other.example/dl/x">go</a>
    </body></html>
    """
    urls = pdf_urls_from_html(html, "https://sci-hub.test/10.1/2")
    assert "https://sci-hub.test/tree/paper.pdf" in urls
    assert "https://other.example/dl/x" in urls


def test_sci_hub_button_onclick():
    html = """<button onclick="location.href='/downloads/abc.pdf'">save</button>"""
    urls = pdf_urls_from_html(html, "https://sci-hub.se/10.1/x")
    assert any(u.endswith("abc.pdf") for u in urls)


def test_prioritize_prefers_pdf_suffix():
    u = prioritize_pdf_urls(
        ["https://x.example/b", "https://x.example/a.pdf", "https://x.example/dl/z"]
    )
    assert u[0].endswith("a.pdf")


def test_is_probably_html():
    assert is_probably_html(b"  <!DOCTYPE html><p>x")
    assert not is_probably_html(b"%PDF-1.4\n")
