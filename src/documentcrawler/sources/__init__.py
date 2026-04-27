"""Source plug-ins. Importing this package registers all built-in sources."""

# Built-ins (importing for side-effect of registration)
from documentcrawler.sources import (  # noqa: F401, E402
    annas_archive,
    arxiv,
    doaj,
    libgen,
    open_access,
    pubmed,
    scihub,
    zlibrary,
)
from documentcrawler.sources.base import (
    Source,
    SourceContext,
    build_source,
    register,
    registry,
)

__all__ = ["Source", "SourceContext", "build_source", "register", "registry"]
