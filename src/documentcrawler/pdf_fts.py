"""PDF Full-Text Search indexing module."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from documentcrawler.pdf import inspect_pdf

if TYPE_CHECKING:
    from documentcrawler.db import Database
    from documentcrawler.models import DocumentRow


def extract_and_index_pdf(db: Database, doc: DocumentRow) -> bool:
    """Extract text content from a downloaded PDF document and index into SQLite FTS5."""
    if not doc.file_path:
        return False

    pdf_path = Path(doc.file_path)
    if not pdf_path.is_file():
        return False

    try:
        info = inspect_pdf(pdf_path)
        text_sample = info.get("text_sample") or ""
        if text_sample:
            db.index_pdf_content(doc.id, text_sample)
            return True
    except Exception:
        pass
    return False


def index_all_downloaded_pdfs(db: Database) -> int:
    """Scan database for all completed documents with PDF files and index their contents."""
    from documentcrawler.models import DocStatus

    docs = db.list_documents(status=DocStatus.DONE, limit=10000)
    indexed_count = 0
    for doc in docs:
        if extract_and_index_pdf(db, doc):
            indexed_count += 1
    return indexed_count
