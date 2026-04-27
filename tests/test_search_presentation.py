from __future__ import annotations

from types import SimpleNamespace

from documentcrawler.presentation import (
    copyable_document_url,
    normalize_document_row,
    normalize_search_hit,
    preferred_queue_url,
)


def _hit(**overrides):
    data = {
        "source": "crossref",
        "title": None,
        "authors": [],
        "year": None,
        "doi": None,
        "isbn": None,
        "abstract": None,
        "url": None,
        "pdf_url": None,
        "container": None,
        "extra": {},
    }
    data.update(overrides)
    authors = list(data["authors"])

    class _Hit(SimpleNamespace):
        @property
        def author_str(self) -> str:
            if not authors:
                return ""
            if len(authors) <= 3:
                return "; ".join(authors)
            return "; ".join(authors[:3]) + f"; +{len(authors) - 3}"

    return _Hit(**data)


def test_normalize_search_hit_prioritizes_human_actionable_fields() -> None:
    hit = _hit(
        source="openalex",
        title="Same paper",
        authors=["Alice Smith", "Bob Jones", "Carol Lee", "Dan Wu"],
        year=2024,
        doi="10.1234/example",
        container="Journal of Useful Results",
        url="https://doi.org/10.1234/example",
        pdf_url="https://example.org/paper.pdf",
        extra={"contributors": {"crossref", "openalex"}},
    )

    view = normalize_search_hit(hit)

    assert view.title == "Same paper"
    assert view.authors == "Alice Smith; Bob Jones; Carol Lee; +1"
    assert view.year == "2024"
    assert view.venue == "Journal of Useful Results"
    assert view.identifier == "DOI 10.1234/example"
    assert view.availability == "PDF + page"
    assert view.sources == "OpenAlex + Crossref"
    assert "DOI: 10.1234/example" in view.preview_text
    assert "Sources: OpenAlex + Crossref" in view.preview_text


def test_normalize_search_hit_keeps_non_doi_results_identifiable() -> None:
    hit = _hit(
        source="annas_archive",
        title="Legacy Book",
        authors=["John Smith"],
        year=1995,
        url="https://annas-archive.org/md5/aaaa1111bbbb2222cccc3333dddd4444",
        extra={
            "md5": "aaaa1111bbbb2222cccc3333dddd4444",
            "mirror": "https://annas-archive.org",
            "meta": "English, pdf, 1.2MB, Old Press, 1995",
        },
    )

    view = normalize_search_hit(hit)

    assert view.identifier == "Anna's Archive MD5 aaaa1111bbbb2222cccc3333dddd4444"
    assert view.availability == "Page"
    assert view.sources == "Anna's Archive"
    assert "Source record: Anna's Archive MD5 aaaa1111bbbb2222cccc3333dddd4444" in view.preview_text


def test_preferred_queue_url_prefers_landing_page_over_pdf() -> None:
    hit = _hit(
        source="openalex",
        doi="10.1234/example",
        url="https://doi.org/10.1234/example",
        pdf_url="https://example.org/paper.pdf",
    )

    assert preferred_queue_url(hit) == "https://doi.org/10.1234/example"


def test_normalize_document_row_uses_human_ordered_fields() -> None:
    row = SimpleNamespace(
        id=42,
        doi="10.1234/example",
        title="Same paper",
        authors=["Alice Smith", "Bob Jones", "Carol Lee", "Dan Wu"],
        year=2024,
        isbn=None,
        keywords=[],
        url="https://doi.org/10.1234/example",
        status=SimpleNamespace(value="failed"),
        file_path=None,
        sha256=None,
        error="download failed",
    )

    view = normalize_document_row(row)

    assert view.title == "Same paper"
    assert view.authors == "Alice Smith; Bob Jones; Carol Lee; +1"
    assert view.year == "2024"
    assert view.identifier == "DOI 10.1234/example"
    assert view.status == "failed"
    assert view.page == "https://doi.org/10.1234/example"
    assert view.file == ""


def test_copyable_document_url_returns_queue_page() -> None:
    row = SimpleNamespace(url="https://doi.org/10.1234/example")

    assert copyable_document_url(row) == "https://doi.org/10.1234/example"
