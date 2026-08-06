"""Pydantic data models used across the pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


class DocumentQuery(BaseModel):
    """A user-supplied reference to a document we want to download."""

    model_config = ConfigDict(extra="ignore")

    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    isbn: str | None = None
    keywords: list[str] = Field(default_factory=list)
    url: str | None = None
    priority: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any([self.doi, self.title, self.isbn, self.url])

    def first_author_last(self) -> str | None:
        if not self.authors:
            return None
        first = self.authors[0].strip()
        if "," in first:
            return first.split(",", 1)[0].strip()
        parts = first.split()
        return parts[-1] if parts else None


class Candidate(BaseModel):
    """A potential download lead returned by a Source."""

    source: str
    url: str
    confidence: float = 1.0
    title: str | None = None
    note: str | None = None
    needs_browser: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class AttemptResult(BaseModel):
    """Outcome of a single source attempt for a single document."""

    source: str
    success: bool
    candidate_url: str | None = None
    http_status: int | None = None
    bytes: int | None = None
    error: str | None = None
    error_kind: str | None = None
    started_at: datetime
    finished_at: datetime


class DocumentRow(BaseModel):
    """SQLite-backed view of a queued document."""

    id: int
    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    isbn: str | None = None
    keywords: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None
    status: DocStatus = DocStatus.PENDING
    file_path: str | None = None
    sha256: str | None = None
    error: str | None = None
    timeout_s: int | None = None
    priority: int = 0
    created_at: datetime
    updated_at: datetime


class SavedSearchRow(BaseModel):
    """A saved search query with its configuration."""

    id: int
    name: str
    query_text: str
    kind: str = "auto"
    sources: list[str] = Field(default_factory=list)
    limit_per_source: int = 15
    created_at: datetime
    updated_at: datetime
