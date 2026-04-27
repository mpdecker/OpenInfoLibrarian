"""Searcher base classes, models, and registry."""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from documentcrawler.fetcher import Fetcher


@dataclass(slots=True)
class SearchHit:
    """A single metadata hit from a searcher."""

    source: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    abstract: str | None = None
    url: str | None = None  # landing page
    pdf_url: str | None = None  # known direct PDF link
    container: str | None = None  # journal / publisher
    score: float = 0.0  # 0..1, source-relative
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_pdf(self) -> bool:
        return bool(self.pdf_url)

    @property
    def author_str(self) -> str:
        if not self.authors:
            return ""
        if len(self.authors) <= 3:
            return "; ".join(self.authors)
        return "; ".join(self.authors[:3]) + f"; +{len(self.authors) - 3}"

    def dedupe_key(self) -> str:
        """Stable key used to merge duplicates across searchers."""
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.isbn:
            return f"isbn:{self.isbn}"
        if self.title:
            t = "".join(c.lower() for c in self.title if c.isalnum())
            return f"title:{t[:80]}|{self.year or ''}"
        return f"id:{id(self)}"


class Searcher(abc.ABC):
    """Abstract base class for metadata searchers."""

    name: str = ""

    def __init__(self, fetcher: Fetcher, options: dict[str, Any] | None = None):
        self.fetcher = fetcher
        self.options = options or {}

    @abc.abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 20,
        kind: str = "auto",
    ) -> list[SearchHit]:
        """Run a search and return up to `limit` hits.

        `kind` is a hint: "auto", "doi", "isbn", "title", "author".
        Searchers may ignore it.
        """


registry: dict[str, type[Searcher]] = {}


def register(name: str) -> Callable[[type[Searcher]], type[Searcher]]:
    def deco(cls: type[Searcher]) -> type[Searcher]:
        cls.name = name
        registry[name] = cls
        return cls

    return deco


def build_searcher(
    name: str,
    fetcher: Fetcher,
    options: dict[str, Any] | None = None,
) -> Searcher | None:
    cls = registry.get(name)
    if cls is None:
        return None
    return cls(fetcher, options or {})
