"""Multi-source metadata search.

Searchers are distinct from Sources:
- A `Source` (in documentcrawler.sources) takes a known reference and tries
  to download the PDF.
- A `Searcher` takes a free-text query and returns rich metadata for the user
  to preview and selectively queue. They can also seed `Source` candidates
  via embedded PDF URLs (`pdf_url` on a SearchHit).
"""

from documentcrawler.searcher.aggregate import MultiSearcher, SearchQuery
from documentcrawler.searcher.base import (
    Searcher,
    SearchHit,
    build_searcher,
    register,
    registry,
)

# Built-in searchers — imported for side-effect of @register on each module.
# Ordered alphabetically; ruff I001 ignored because the order matters
# (registration side-effects).
from documentcrawler.searcher import annas as _annas  # noqa: F401, E402, I001
from documentcrawler.searcher import arxiv as _arxiv  # noqa: F401, E402
from documentcrawler.searcher import core as _core  # noqa: F401, E402
from documentcrawler.searcher import crossref as _crossref  # noqa: F401, E402
from documentcrawler.searcher import doi_org as _doi_org  # noqa: F401, E402
from documentcrawler.searcher import jstor as _jstor  # noqa: F401, E402
from documentcrawler.searcher import libgen as _libgen  # noqa: F401, E402
from documentcrawler.searcher import national_archives as _national_archives  # noqa: F401, E402
from documentcrawler.searcher import openalex as _openalex  # noqa: F401, E402
from documentcrawler.searcher import openlibrary as _openlibrary  # noqa: F401, E402
from documentcrawler.searcher import publisher_metadata as _publisher_metadata  # noqa: F401, E402
from documentcrawler.searcher import scihub_search as _scihub_search  # noqa: F401, E402
from documentcrawler.searcher import semantic_scholar as _semantic_scholar  # noqa: F401, E402
from documentcrawler.searcher import zlibrary as _zlibrary  # noqa: F401, E402

__all__ = [
    "MultiSearcher",
    "SearchQuery",
    "SearchHit",
    "Searcher",
    "build_searcher",
    "register",
    "registry",
]
