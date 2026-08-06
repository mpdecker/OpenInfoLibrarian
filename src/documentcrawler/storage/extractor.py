"""Lightweight PDF text and abstract extractor for downloaded documents."""

from __future__ import annotations

import re
from pathlib import Path


def extract_pdf_text(file_path: Path | str, max_pages: int = 10) -> str:
    """Extract plain text snippets from a PDF file.

    Uses standard PDF content stream parsing to extract text blocks.
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return ""

    try:
        content = path.read_bytes()
    except OSError:
        return ""

    # Extract text from streams between BT (Begin Text) and ET (End Text)
    text_blocks: list[str] = []
    # Match text objects in PDF streams
    matches = re.findall(rb"BT\s*(.*?)\s*ET", content, re.DOTALL)

    for match in matches[: max_pages * 20]:
        # Extract strings inside parentheses (Tj / TJ operators)
        strings = re.findall(rb"\((.*?)\)", match)
        for s in strings:
            try:
                decoded = s.decode("utf-8", errors="ignore").strip()
                if len(decoded) > 2 and re.search(r"[a-zA-Z0-9]", decoded):
                    text_blocks.append(decoded)
            except Exception:
                continue

    cleaned = " ".join(text_blocks)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
