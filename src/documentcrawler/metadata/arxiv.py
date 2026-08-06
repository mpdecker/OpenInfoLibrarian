"""arXiv API metadata lookup helper."""

from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import quote

from selectolax.parser import HTMLParser

from documentcrawler.fetcher import Fetcher
from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)

_ARXIV_API_BASE = "http://export.arxiv.org/api/query"
_ARXIV_ID_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+/\d{7})", re.IGNORECASE)


def extract_arxiv_id(val: str | None) -> str | None:
    """Extract clean arXiv ID (e.g. '1706.03762') from string or URL."""
    if not val:
        return None
    match = _ARXIV_ID_RE.search(val)
    return match.group(1) if match else None


async def arxiv_lookup(fetcher: Fetcher, arxiv_id: str) -> dict[str, Any] | None:
    """Query arXiv API for metadata by arXiv ID."""
    clean_id = extract_arxiv_id(arxiv_id) or arxiv_id
    url = f"{_ARXIV_API_BASE}?id_list={quote(clean_id)}"
    try:
        xml = await fetcher.get_text(url)
        tree = HTMLParser(xml)
        entry = tree.css_first("entry")
        if not entry:
            return None

        title_node = entry.css_first("title")
        title = title_node.text().strip().replace("\n", " ") if title_node else None

        authors = [node.text().strip() for node in entry.css("author name") if node.text()]

        published_node = entry.css_first("published")
        year: int | None = None
        if published_node and len(published_node.text()) >= 4:
            with contextlib.suppress(ValueError):
                year = int(published_node.text()[:4])

        summary_node = entry.css_first("summary")
        summary = summary_node.text().strip() if summary_node else None

        pdf_url = f"https://arxiv.org/pdf/{clean_id}.pdf"

        return {
            "arxiv_id": clean_id,
            "title": title,
            "authors": authors,
            "year": year,
            "abstract": summary,
            "pdf_url": pdf_url,
            "url": f"https://arxiv.org/abs/{clean_id}",
        }
    except Exception as e:
        log.debug("arxiv lookup failed for %s: %s", arxiv_id, e)
        return None



