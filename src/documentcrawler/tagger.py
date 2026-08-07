"""Automated academic paper topic and subject classifier."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.models import DocumentRow

_TOPIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Computer Science & AI",
        re.compile(
            r"\b(artificial intelligence|machine learning|deep learning|neural network|"
            r"computer vision|natural language|nlp|transformer|llm|algorithm|software|"
            r"cs\.[a-z]+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Physics & Astronomy",
        re.compile(
            r"\b(quantum|relativity|astronomy|astrophysics|particle|thermodynamics|"
            r"optics|superconductor|gravitation|cosmology|phys\.[a-z]+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Mathematics & Statistics",
        re.compile(
            r"\b(topology|algebra|geometry|calculus|probability|stochastic|theorem|"
            r"differential equation|manifold|math\.[a-z]+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Biology & Medicine",
        re.compile(
            r"\b(genomics|genome|dna|rna|protein|cellular|molecular|oncology|"
            r"pharmacology|neuroscience|clinical|pathology|biorxiv|medrxiv)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Economics & Finance",
        re.compile(
            r"\b(macroeconomics|microeconomics|econometrics|inflation|monetary|"
            r"fiscal|gdp|asset pricing|portfolio|stock market|econ\.[a-z]+)\b",
            re.IGNORECASE,
        ),
    ),
]


def classify_document_topics(doc: DocumentRow) -> list[str]:
    """Analyze document title, keywords, and extra metadata to assign standardized subject category tags."""
    text_parts: list[str] = []
    if doc.title:
        text_parts.append(doc.title)
    if doc.keywords:
        text_parts.extend(doc.keywords)
    if doc.extra:
        search_source = doc.extra.get("search_source")
        if search_source:
            text_parts.append(str(search_source))

    full_text = " ".join(text_parts)
    if not full_text.strip():
        return []

    tags: list[str] = []
    for category, pattern in _TOPIC_PATTERNS:
        if pattern.search(full_text):
            tags.append(category)

    return tags
