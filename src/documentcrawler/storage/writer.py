"""Atomic file writes + verification + dedupe by sha256."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

PDF_MAGIC = b"%PDF-"


@dataclass
class WriteResult:
    path: Path
    sha256: str
    bytes: int
    deduped: bool = False


class InvalidPDFError(ValueError):
    """Raised when downloaded bytes do not look like a PDF."""


def looks_like_pdf(data: bytes) -> bool:
    return data[:5] == PDF_MAGIC or data[:1024].lstrip().startswith(PDF_MAGIC)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_pdf(data: bytes, *, min_bytes: int = 20480) -> None:
    if len(data) < min_bytes:
        raise InvalidPDFError(f"file too small: {len(data)} bytes < {min_bytes}")
    if not looks_like_pdf(data):
        raise InvalidPDFError("missing %PDF- magic bytes")


def atomic_write(path: Path, data: bytes) -> Path:
    """Write `data` to `path` atomically (tmp + rename), creating dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dl-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def disambiguate(path: Path) -> Path:
    """If `path` exists, append _1, _2, ... before the extension."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1
