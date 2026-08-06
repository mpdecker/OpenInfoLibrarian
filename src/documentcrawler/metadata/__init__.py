from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.metadata.arxiv import arxiv_lookup, extract_arxiv_id
    from documentcrawler.metadata.enricher import EnrichedMetadata, MetadataEnricher

__all__ = ["EnrichedMetadata", "MetadataEnricher", "arxiv_lookup", "extract_arxiv_id"]


def __getattr__(name: str):
    if name in __all__:
        from documentcrawler.metadata.arxiv import arxiv_lookup, extract_arxiv_id
        from documentcrawler.metadata.enricher import EnrichedMetadata, MetadataEnricher

        exports = {
            "EnrichedMetadata": EnrichedMetadata,
            "MetadataEnricher": MetadataEnricher,
            "arxiv_lookup": arxiv_lookup,
            "extract_arxiv_id": extract_arxiv_id,
        }
        return exports[name]
    raise AttributeError(name)
