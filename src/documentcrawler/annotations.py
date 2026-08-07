"""Document annotations and reading notes manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from documentcrawler.db import Database


def annotate_document(
    db: Database,
    doc_id: int,
    *,
    rating: int | None = None,
    review_status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Attach or update reading annotations and star ratings for a document."""
    doc = db.get(doc_id)
    if doc is None:
        raise ValueError(f"Document #{doc_id} not found in database.")

    if rating is not None and not (0 <= rating <= 5):
        raise ValueError("Rating must be an integer between 0 and 5 stars.")

    if review_status is not None:
        valid_statuses = {"unread", "reading", "reviewed", "archived"}
        if review_status.lower() not in valid_statuses:
            raise ValueError(f"Invalid review status '{review_status}'. Valid choices: {', '.join(valid_statuses)}.")

    return db.set_annotation(
        doc_id,
        rating=rating,
        review_status=review_status.lower() if review_status else None,
        notes=notes,
    )
