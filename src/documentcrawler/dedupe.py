"""Automated document deduplication and consolidation engine."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documentcrawler.db import Database
    from documentcrawler.models import DocumentRow


def _normalize_title_str(t: str | None) -> str:
    if not t:
        return ""
    t_clean = re.sub(r"[^\w\s]", "", t.lower())
    words = sorted(t_clean.split())
    return " ".join(words)


def _similarity(s1: str, s2: str) -> float:
    """Calculate token-sort similarity score between two normalized strings [0.0..1.0]."""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    w1, w2 = set(s1.split()), set(s2.split())
    intersection = len(w1 & w2)
    union = len(w1 | w2)
    return intersection / union if union > 0 else 0.0


def find_duplicate_clusters(db: Database, threshold: float = 0.85) -> list[list[DocumentRow]]:
    """Scan all database documents and return clusters of duplicate DocumentRow items."""
    docs = db.list_documents(limit=10000)
    if len(docs) < 2:
        return []

    clusters: list[list[DocumentRow]] = []
    visited: set[int] = set()

    # Index by DOI
    doi_map: dict[str, list[DocumentRow]] = defaultdict(list)
    for d in docs:
        if d.doi:
            doi_map[d.doi.lower().strip()].append(d)

    for doi, group in doi_map.items():
        if len(group) > 1:
            clusters.append(group)
            visited.update(d.id for d in group)

    # Index by normalized Title
    unvisited_docs = [d for d in docs if d.id not in visited and d.title]
    for i in range(len(unvisited_docs)):
        d1 = unvisited_docs[i]
        if d1.id in visited:
            continue
        t1_norm = _normalize_title_str(d1.title)
        if not t1_norm:
            continue

        current_cluster = [d1]
        for j in range(i + 1, len(unvisited_docs)):
            d2 = unvisited_docs[j]
            if d2.id in visited:
                continue
            t2_norm = _normalize_title_str(d2.title)
            score = _similarity(t1_norm, t2_norm)
            if score >= threshold:
                current_cluster.append(d2)

        if len(current_cluster) > 1:
            clusters.append(current_cluster)
            visited.update(d.id for d in current_cluster)

    return clusters
