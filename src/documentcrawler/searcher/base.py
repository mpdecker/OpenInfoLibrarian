"""Searcher base classes, models, and registry."""

from __future__ import annotations

import abc
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from documentcrawler.fetcher import Fetcher

_TITLE_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "of", "in", "for", "on", "to", "and", "is", "are",
        "with", "from", "at", "by", "or", "as", "be", "it", "its", "not",
        "that", "this", "was", "were", "but", "into", "over", "their",
        "has", "had", "been", "also", "can", "may", "which", "will",
    }
)
_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]+")


def _normalize_title(title: str) -> str:
    lowered = _TITLE_NORMALIZE_RE.sub(" ", title.lower())
    words = [w for w in lowered.split() if w not in _TITLE_STOP_WORDS and len(w) > 1]
    return " ".join(sorted(words))


def _first_author_surname(authors: list[str]) -> str:
    if not authors:
        return ""
    name = authors[0].strip()
    if "," in name:
        return name.split(",")[0].strip().lower()
    parts = name.split()
    return parts[-1].lower() if parts else name.lower()


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

    @property
    def _non_null_field_count(self) -> int:
        n = 0
        if self.doi:
            n += 1
        if self.title:
            n += 1
        if self.authors:
            n += 1
        if self.year is not None:
            n += 1
        if self.isbn:
            n += 1
        if self.abstract:
            n += 1
        if self.container:
            n += 1
        if self.url:
            n += 1
        if self.pdf_url:
            n += 1
        return n

    def dedupe_key(self) -> str:
        """Stable key used to merge duplicates across searchers.

        Hierarchy: DOI > ISBN > normalized-title+year > normalized-title+author.

        Title normalization strips stopwords and non-alphanumeric characters,
        then sorts remaining words.  This allows "An Introduction to Machine
        Learning" and "Machine Learning, An Introduction" to match.
        """
        if self.doi:
            doi_cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", self.doi, flags=re.IGNORECASE)
            return f"doi:{doi_cleaned.lower()}"
        if self.isbn:
            return f"isbn:{self.isbn.strip().replace('-', '').replace(' ', '')}"
        if self.title:
            t = _normalize_title(self.title)
            if t and self.year:
                return f"title:{t}|{self.year}"
            if t and self.authors:
                au = _first_author_surname(self.authors)
                if au:
                    return f"title:{t}|author:{au}"
            return f"title:{t}" if t else f"id:{id(self)}"
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
