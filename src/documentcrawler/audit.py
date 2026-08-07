"""Metadata quality audit scanner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from documentcrawler.db import Database


def run_metadata_health_audit(db: Database) -> dict[str, Any]:
    """Run a comprehensive audit of metadata health, completeness, and file links."""
    return db.audit_metadata_health()
