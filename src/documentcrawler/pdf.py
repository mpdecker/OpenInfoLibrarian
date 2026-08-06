"""PDF metadata and structural inspection utility."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from documentcrawler.storage.writer import InvalidPDFError, verify_pdf


def inspect_pdf(filepath: str | Path) -> dict[str, Any]:
    """Inspect a PDF file and extract structural properties, catalog metadata, and text sample."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    data = path.read_bytes()
    verify_pdf(data, min_bytes=100)

    header = data[:20].decode("ascii", errors="ignore")
    version_match = re.search(r"%PDF-(\d+\.\d+)", header)
    pdf_version = version_match.group(1) if version_match else "unknown"

    # Search for page count from /Count <int> in catalog object or count /Page objects
    page_count = 1
    count_match = re.search(r"/Count\s+(\d+)", data.decode("latin1", errors="ignore"))
    if count_match:
        page_count = int(count_match.group(1))
    else:
        pages = re.findall(r"/Type\s*/Page\b", data.decode("latin1", errors="ignore"))
        if pages:
            page_count = len(pages)

    # Extract metadata catalog entries (Title, Author, Creator, Producer)
    metadata: dict[str, str] = {}
    latin1_text = data.decode("latin1", errors="ignore")
    for key in ("Title", "Author", "Creator", "Producer"):
        m = re.search(r"/" + key + r"\s*\((.*?)\)", latin1_text)
        if m:
            metadata[key.lower()] = m.group(1)

    # Extract raw readable ASCII/latin1 text snippet
    text_matches = re.findall(r"\(([^()]{4,})\)\s*Tj", latin1_text)
    sample_snippet = " ".join(text_matches[:30]).strip() if text_matches else ""

    return {
        "file_path": str(path),
        "file_size_bytes": len(data),
        "file_size_kb": round(len(data) / 1024, 2),
        "pdf_version": pdf_version,
        "page_count": page_count,
        "is_valid_pdf": True,
        "metadata": metadata,
        "text_sample": sample_snippet[:500],
    }
