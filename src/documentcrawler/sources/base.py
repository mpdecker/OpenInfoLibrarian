"""Source plug-in base class and a tiny registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from documentcrawler.config import SourceConfig
from documentcrawler.fetcher import Fetcher
from documentcrawler.metadata.enricher import EnrichedMetadata
from documentcrawler.models import Candidate, DocumentQuery


@dataclass
class SourceContext:
    """Per-document context handed to a Source."""

    query: DocumentQuery
    metadata: EnrichedMetadata
    fetcher: Fetcher
    options: dict[str, Any]


class Source(ABC):
    """Abstract source. Implementations register themselves via @register."""

    name: str = "base"

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = options or {}

    @abstractmethod
    async def search(self, ctx: SourceContext) -> list[Candidate]:
        """Return zero or more candidate URLs that may yield the document."""

    async def fetch(self, candidate: Candidate, ctx: SourceContext) -> bytes | None:
        """Resolve interstitial HTML, follow mirror links, then download bytes."""
        return await ctx.fetcher.download_document(candidate.url)


_FACTORY = Callable[[SourceConfig], Source]
registry: dict[str, _FACTORY] = {}


def register(name: str) -> Callable[[type[Source]], type[Source]]:
    def decorator(cls: type[Source]) -> type[Source]:
        cls.name = name

        def factory(cfg: SourceConfig) -> Source:
            return cls(options=cfg.options)

        registry[name] = factory
        return cls

    return decorator


def build_source(name: str, cfg: SourceConfig) -> Source | None:
    factory = registry.get(name)
    return factory(cfg) if factory else None


# Re-export for type hints
SearchFn = Callable[[SourceContext], Awaitable[list[Candidate]]]
