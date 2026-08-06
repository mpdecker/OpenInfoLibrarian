"""Unit tests for metadata normalizer engine."""

from documentcrawler.metadata.normalizer import (
    normalize_author_name,
    normalize_authors,
    normalize_doi,
    normalize_title,
)


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1000/182") == "10.1000/182"
    assert normalize_doi("http://dx.doi.org/10.1000/182") == "10.1000/182"
    assert normalize_doi("doi: 10.1000/182 ") == "10.1000/182"
    assert normalize_doi("10.1000/182") == "10.1000/182"
    assert normalize_doi(None) is None


def test_normalize_author_name():
    assert normalize_author_name("John A. Smith") == "Smith, John A."
    assert normalize_author_name("Smith, John A.") == "Smith, John A."
    assert normalize_author_name("  Einstein,   Albert  ") == "Einstein, Albert"


def test_normalize_authors():
    raw = ["John Smith", "Smith, John", "Albert Einstein"]
    norm = normalize_authors(raw)
    assert norm == ["Smith, John", "Einstein, Albert"]


def test_normalize_title():
    assert normalize_title("  \"Attention Is All You Need\"  ") == "Attention Is All You Need"
    assert normalize_title(None) is None
