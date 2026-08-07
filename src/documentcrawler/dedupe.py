"""Automated document deduplication and consolidation engine."""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from documentcrawler.db import Database
    from documentcrawler.models import DocumentRow

DedupeStrategy = Literal["exact", "fuzzy", "smart"]


def _normalize_str(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def _normalize_title_tokens(t: str | None) -> set[str]:
    if not t:
        return set()
    clean = _normalize_str(t)
    return set(clean.split())


def _extract_surnames(authors: list[str]) -> set[str]:
    surnames = set()
    for a in authors:
        a_clean = a.strip()
        if "," in a_clean:
            surnames.add(_normalize_str(a_clean.split(",")[0]))
        else:
            parts = a_clean.split()
            if parts:
                surnames.add(_normalize_str(parts[-1]))
    return surnames - {""}


def title_similarity(t1: str | None, t2: str | None) -> float:
    """Calculate combined sequence + token similarity score between two titles [0.0..1.0]."""
    if not t1 or not t2:
        return 0.0
    norm1, norm2 = _normalize_str(t1), _normalize_str(t2)
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0

    seq_score = SequenceMatcher(None, norm1, norm2).ratio()
    tokens1, tokens2 = _normalize_title_tokens(t1), _normalize_title_tokens(t2)
    union = len(tokens1 | tokens2)
    jaccard_score = (len(tokens1 & tokens2) / union) if union > 0 else 0.0

    return round(0.5 * seq_score + 0.5 * jaccard_score, 3)


def composite_similarity(d1: DocumentRow, d2: DocumentRow) -> float:
    """Calculate multi-field similarity score across title, authors, and year."""
    if not d1.title or not d2.title:
        return 0.0

    t_sim = title_similarity(d1.title, d2.title)
    if t_sim < 0.6:
        return t_sim

    # Author similarity
    a1, a2 = _extract_surnames(d1.authors), _extract_surnames(d2.authors)
    if a1 and a2:
        author_sim = len(a1 & a2) / len(a1 | a2)
    else:
        author_sim = 0.5  # Neutral if one has no author info

    # Year compatibility
    year_penalty = 0.0
    if d1.year and d2.year:
        diff = abs(d1.year - d2.year)
        if diff > 1:
            year_penalty = 0.2
        elif diff == 0:
            year_penalty = -0.05  # Bonus for exact year match

    score = (0.7 * t_sim) + (0.3 * author_sim) - year_penalty
    return max(0.0, min(1.0, round(score, 3)))


def select_primary_document(cluster: list[DocumentRow]) -> DocumentRow:
    """Select the best candidate to serve as the primary document for merging."""
    if not cluster:
        raise ValueError("Cannot select primary document from empty cluster")

    def _score(doc: DocumentRow) -> tuple[int, int, int, int]:
        is_done = 1 if doc.status.value == "done" else 0
        has_doi = 1 if doc.doi else 0
        meta_completeness = (
            (1 if doc.title else 0) +
            (len(doc.authors)) +
            (1 if doc.year else 0) +
            (1 if doc.file_path else 0)
        )
        return (is_done, has_doi, meta_completeness, -doc.id)

    return max(cluster, key=_score)


def find_duplicate_clusters(
    db: Database,
    threshold: float = 0.85,
    strategy: DedupeStrategy = "smart",
) -> list[dict[str, Any]]:
    """Scan database documents and return rich duplicate cluster objects."""
    docs = db.list_documents(limit=10000)
    if len(docs) < 2:
        return []

    clusters: list[dict[str, Any]] = []
    visited: set[int] = set()

    # 1. Exact DOI match
    doi_map: dict[str, list[DocumentRow]] = defaultdict(list)
    for d in docs:
        if d.doi:
            doi_map[d.doi.lower().strip()].append(d)

    for doi, group in doi_map.items():
        if len(group) > 1:
            primary = select_primary_document(group)
            secondaries = [d for d in group if d.id != primary.id]
            clusters.append({
                "match_reason": "exact_doi",
                "confidence": 1.0,
                "primary": primary,
                "secondaries": secondaries,
                "all_docs": group,
            })
            visited.update(d.id for d in group)

    # 2. Exact SHA256 PDF match
    if strategy in ("exact", "smart"):
        sha_map: dict[str, list[DocumentRow]] = defaultdict(list)
        for d in docs:
            if d.id not in visited and d.sha256:
                sha_map[d.sha256].append(d)

        for sha, group in sha_map.items():
            if len(group) > 1:
                primary = select_primary_document(group)
                secondaries = [d for d in group if d.id != primary.id]
                clusters.append({
                    "match_reason": "exact_sha256",
                    "confidence": 1.0,
                    "primary": primary,
                    "secondaries": secondaries,
                    "all_docs": group,
                })
                visited.update(d.id for d in group)

    if strategy == "exact":
        return clusters

    # 3. Fuzzy Composite Matching (Title + Author + Year)
    unvisited = [d for d in docs if d.id not in visited and d.title]
    for i in range(len(unvisited)):
        d1 = unvisited[i]
        if d1.id in visited:
            continue

        matched_docs = [d1]
        best_score = 0.0

        for j in range(i + 1, len(unvisited)):
            d2 = unvisited[j]
            if d2.id in visited:
                continue

            score = composite_similarity(d1, d2)
            if score >= threshold:
                matched_docs.append(d2)
                best_score = max(best_score, score)

        if len(matched_docs) > 1:
            primary = select_primary_document(matched_docs)
            secondaries = [d for d in matched_docs if d.id != primary.id]
            clusters.append({
                "match_reason": "fuzzy_composite",
                "confidence": best_score,
                "primary": primary,
                "secondaries": secondaries,
                "all_docs": matched_docs,
            })
            visited.update(d.id for d in matched_docs)

    return clusters
