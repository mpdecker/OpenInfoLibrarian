"""Queue Priority Calculation Module."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.models import DocumentRow


def calculate_priority_score(doc: DocumentRow) -> int:
    """Calculate dynamic queue priority score for a DocumentRow.

    Scoring factors:
    - Base score: doc.priority (explicitly assigned priority)
    - Has DOI: +50 points
    - Has Year: +20 points (recent years get extra +1 per year past 2000)
    - Has Title: +30 points
    - Has Authors: +20 points
    - Has ISBN or URL: +10 points
    """
    score = doc.priority or 0

    if doc.doi:
        score += 50
    if doc.title and len(doc.title.strip()) > 5:
        score += 30
    if doc.authors:
        score += 20
    if doc.year:
        score += 20
        if doc.year >= 2000:
            score += min(doc.year - 2000, 30)
    if doc.isbn or doc.url:
        score += 10

    return score
