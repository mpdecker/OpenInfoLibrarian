"""documentcrawler: find and download academic documents from many sources."""

from __future__ import annotations

from documentcrawler.config import Config
from documentcrawler.db import Database
from documentcrawler.fetcher import Fetcher
from documentcrawler.models import DocumentQuery, DocumentRow
from documentcrawler.pipeline import Pipeline

__version__ = "0.2.0"
__all__ = [
    "Config",
    "Database",
    "DocumentQuery",
    "DocumentRow",
    "Fetcher",
    "Pipeline",
    "__version__",
]
