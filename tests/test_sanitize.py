from documentcrawler.utils.sanitize import (
    extract_arxiv_id,
    normalize_doi,
    normalize_isbn,
    safe_filename_part,
    title_slug,
)


def test_normalize_doi_strips_url_prefix():
    assert normalize_doi("https://doi.org/10.1038/s41586-020-2649-2") == "10.1038/s41586-020-2649-2"
    assert normalize_doi("doi:10.1000/xyz123") == "10.1000/xyz123"
    assert normalize_doi("DOI: 10.1000/xyz123  ") == "10.1000/xyz123"


def test_normalize_doi_returns_none_for_garbage():
    assert normalize_doi(None) is None
    assert normalize_doi("not a doi") is None


def test_normalize_isbn():
    assert normalize_isbn("978-3-16-148410-0") == "9783161484100"
    assert normalize_isbn(None) is None
    assert normalize_isbn("0-306-40615-2X") == "0306406152X"


def test_extract_arxiv_id():
    assert extract_arxiv_id("arXiv:1706.03762") == "1706.03762"
    assert extract_arxiv_id("see 2304.12345v2 for context") == "2304.12345"
    assert extract_arxiv_id("nothing") is None


def test_title_slug_truncation():
    assert title_slug("Attention Is All You Need") == "attention-is-all-you-need"
    long = "A" * 200
    assert len(title_slug(long, max_len=30)) <= 30


def test_safe_filename_part_strips_unicode_and_punct():
    assert safe_filename_part("Müller, Hans-Jürgen") == "Muller_Hans-Jurgen"
    assert safe_filename_part("") == "unknown"
