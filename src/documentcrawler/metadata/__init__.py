from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.metadata.enricher import EnrichedMetadata, MetadataEnricher

__all__ = ["EnrichedMetadata", "MetadataEnricher"]


def __getattr__(name: str):
    if name in __all__:
        from documentcrawler.metadata.enricher import EnrichedMetadata, MetadataEnricher

        exports = {
            "EnrichedMetadata": EnrichedMetadata,
            "MetadataEnricher": MetadataEnricher,
        }
        return exports[name]
    raise AttributeError(name)
