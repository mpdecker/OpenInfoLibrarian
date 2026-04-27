import pytest

from documentcrawler.storage.writer import (
    InvalidPDFError,
    atomic_write,
    disambiguate,
    looks_like_pdf,
    sha256_of,
    verify_pdf,
)


def test_looks_like_pdf():
    assert looks_like_pdf(b"%PDF-1.4\n...")
    assert not looks_like_pdf(b"<html><body>not a pdf</body></html>")


def test_verify_pdf_ok():
    data = b"%PDF-1.4\n" + b"x" * 30000
    verify_pdf(data, min_bytes=20480)


def test_verify_pdf_too_small():
    with pytest.raises(InvalidPDFError):
        verify_pdf(b"%PDF-1.4 short", min_bytes=20480)


def test_verify_pdf_bad_magic():
    with pytest.raises(InvalidPDFError):
        verify_pdf(b"<html>" + b"x" * 30000, min_bytes=20480)


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "sub" / "a.pdf"
    atomic_write(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_disambiguate_existing(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"a")
    new = disambiguate(p)
    assert new.name == "x_1.pdf"
    new.write_bytes(b"b")
    new2 = disambiguate(p)
    assert new2.name == "x_2.pdf"


def test_sha256_deterministic():
    assert sha256_of(b"abc") == sha256_of(b"abc")
    assert sha256_of(b"abc") != sha256_of(b"abd")
