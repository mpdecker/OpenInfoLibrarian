"""Shared error taxonomy for documentcrawler."""

from __future__ import annotations


class DocumentCrawlerError(Exception):
    """Base exception for all documentcrawler-specific errors."""


class ConfigError(DocumentCrawlerError):
    """Configuration validation or load failures."""


class AcquisitionError(DocumentCrawlerError):
    """Pipeline acquisition failure — can be transient or permanent."""

    def __init__(self, message: str = "", *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class SourceError(AcquisitionError):
    """Per-source acquisition failure."""

    def __init__(self, source: str, message: str = "", *, permanent: bool = False):
        super().__init__(f"[{source}] {message}" if message else source,
                         permanent=permanent)
        self.source = source


class CLIError(DocumentCrawlerError):
    """CLI usage or input error."""
