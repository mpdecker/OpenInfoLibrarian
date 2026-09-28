"""High-level metadata enrichment combining Crossref / OpenAlex / Unpaywall."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from documentcrawler.fetcher import Fetcher
from documentcrawler.metadata.arxiv import arxiv_id_from_doi, arxiv_lookup, extract_arxiv_id
from documentcrawler.metadata.crossref import crossref_lookup, crossref_search
from documentcrawler.metadata.openalex import openalex_lookup
from documentcrawler.metadata.publication_resolver import PublicationResolver
from documentcrawler.metadata.unpaywall import unpaywall_lookup
from documentcrawler.models import DocumentQuery
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi

log = get_logger(__name__)


@dataclass
class EnrichedMetadata:
    doi: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    url: str | None = None
    journal: str | None = None
    publisher: str | None = None
    isbn: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    oa_urls: list[str] = field(default_factory=list)
    enrich_providers: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class MetadataEnricher:
    def __init__(self, fetcher: Fetcher, *, unpaywall_email: str | None = None,
                 crossref_mailto: str | None = None,
                 openalex_mailto: str | None = None,
                 semantic_scholar_api_key: str | None = None):
        self.fetcher = fetcher
        self.unpaywall_email = unpaywall_email
        self.crossref_mailto = crossref_mailto
        self.openalex_mailto = openalex_mailto
        self.publication_resolver = PublicationResolver(
            fetcher,
            crossref_mailto=crossref_mailto,
            openalex_mailto=openalex_mailto,
            semantic_scholar_api_key=semantic_scholar_api_key,
        )

    async def enrich(self, q: DocumentQuery) -> EnrichedMetadata:
        meta = EnrichedMetadata(
            doi=normalize_doi(q.doi),
            title=q.title,
            authors=list(q.authors),
            year=q.year,
            isbn=q.isbn,
            url=q.url,
            journal=_query_hint(q.extra, "journal", "booktitle", "container", "venue"),
            publisher=_query_hint(q.extra, "publisher"),
        )

        # An explicit DOI in the query outranks everything except the
        # explicit year: the per-DOI lookups below hit the exact record,
        # while the publication resolver only fuzzy-matches on title/author
        # and can return a different paper entirely.
        doi_from_query = meta.doi is not None

        if q.title or q.authors or q.keywords or q.extra or q.isbn or q.doi:
            try:
                resolved = await self.publication_resolver.resolve(q)
                if resolved:
                    meta.doi = meta.doi or resolved.doi
                    meta.isbn = meta.isbn or resolved.isbn
                    meta.title = meta.title or resolved.title
                    meta.authors = meta.authors or list(resolved.authors)
                    meta.year = meta.year or resolved.year
                    meta.url = meta.url or resolved.url
                    meta.journal = meta.journal or resolved.journal
                    meta.publisher = meta.publisher or resolved.publisher
                    meta.raw["publication_search"] = {
                        "source": resolved.source,
                        "score": resolved.score,
                        "url": resolved.url,
                        **dict(resolved.raw),
                    }
            except Exception as e:
                log.debug("publication resolver failed: %s", e)

        if not meta.doi and (q.title or q.authors):
            try:
                hit = await crossref_search(
                    self.fetcher,
                    title=q.title,
                    author=" ".join(q.authors[:1]) if q.authors else None,
                    year=q.year,
                    mailto=self.crossref_mailto,
                )
                if hit:
                    meta.doi = hit.get("DOI") or meta.doi
                    meta.title = meta.title or _join_first(hit.get("title"))
                    meta.authors = meta.authors or _crossref_authors(hit.get("author") or [])
                    meta.year = meta.year or _crossref_year(hit)
                    meta.url = meta.url or hit.get("URL")
                    meta.journal = _join_first(hit.get("container-title"))
                    meta.publisher = hit.get("publisher")
                    meta.raw["crossref_search"] = hit
            except Exception as e:
                log.debug("crossref search failed: %s", e)

        if not meta.doi and q.url:
            # A pasted PubMed link carries no DOI — recover it via
            # esummary so the regular Crossref/OpenAlex/Unpaywall chain
            # (and its OA URLs) can fire for this document too.
            doi = await _doi_from_pubmed_url(self.fetcher, q.url)
            if doi:
                meta.doi = doi

        if meta.doi:
            tasks = [
                crossref_lookup(self.fetcher, meta.doi, mailto=self.crossref_mailto),
                openalex_lookup(self.fetcher, meta.doi, mailto=self.openalex_mailto),
            ]
            if self.unpaywall_email:
                tasks.append(unpaywall_lookup(self.fetcher, meta.doi, self.unpaywall_email))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            cr = results[0] if len(results) > 0 and isinstance(results[0], dict) else None
            oa = results[1] if len(results) > 1 and isinstance(results[1], dict) else None
            up = results[2] if len(results) > 2 and isinstance(results[2], dict) else None
            meta.enrich_providers = [
                name for name, val in (("crossref", cr), ("openalex", oa), ("unpaywall", up))
                if val is not None
            ]
            for name, res in zip(
                ("crossref", "openalex", "unpaywall"), results, strict=False
            ):
                if isinstance(res, BaseException):
                    log.debug("enrich: %s lookup raised: %s", name, res)
                elif res is None:
                    log.debug("enrich: %s lookup returned no data", name)

            if cr:
                _apply(meta, _cr_fields(cr), override=doi_from_query, protect_year=q.year,
                       prefer_canonical_title=_looks_like_citation(q.title))
                meta.raw["crossref"] = cr

            if oa:
                meta.raw["openalex"] = oa
                _apply(meta, _oa_fields(oa), override=doi_from_query, protect_year=q.year,
                       prefer_canonical_title=_looks_like_citation(q.title))
                meta.url = meta.url or oa.get("doi") or oa.get("id")
                if oa.get("primary_location"):
                    url = oa["primary_location"].get("pdf_url")
                    if url:
                        meta.oa_urls.append(url)
                for loc in oa.get("locations", []) or []:
                    if loc.get("pdf_url"):
                        meta.oa_urls.append(loc["pdf_url"])
                ids = oa.get("ids") or {}
                if ids.get("pmid"):
                    meta.pmid = str(ids["pmid"]).rsplit("/", 1)[-1]
                if ids.get("pmcid"):
                    meta.pmcid = str(ids["pmcid"]).rsplit("/", 1)[-1]

            if up:
                meta.raw["unpaywall"] = up
                best = up.get("best_oa_location") or {}
                for key in ("url_for_pdf", "url"):
                    if best.get(key):
                        meta.oa_urls.append(best[key])
                for loc in up.get("oa_locations") or []:
                    for key in ("url_for_pdf", "url"):
                        if loc.get(key):
                            meta.oa_urls.append(loc[key])

        arxiv_id = (
            extract_arxiv_id(q.url)
            or extract_arxiv_id(q.title)
            or arxiv_id_from_doi(q.doi)
        )
        if arxiv_id:
            try:
                ar = await arxiv_lookup(self.fetcher, arxiv_id)
                if ar:
                    meta.arxiv_id = ar["arxiv_id"]
                    meta.title = meta.title or ar["title"]
                    meta.authors = meta.authors or ar["authors"]
                    meta.year = meta.year or ar["year"]
                    if ar.get("pdf_url"):
                        meta.oa_urls.append(ar["pdf_url"])
                    meta.raw["arxiv"] = ar
                    meta.enrich_providers.append("arxiv")
            except Exception as e:
                log.debug("arxiv lookup failed: %s", e)

        # de-dupe oa urls preserving order
        seen: set[str] = set()
        meta.oa_urls = [u for u in meta.oa_urls if not (u in seen or seen.add(u))]
        return meta


def _query_hint(extra: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_PUBMED_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE)

# "(2017)." / "et al." / very long strings are pasted citations, not real
# titles — the matched record's canonical title should replace them.
_CITATION_LIKE_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)|\bet\.?\s+al\b", re.IGNORECASE)


def _looks_like_citation(title: str | None) -> bool:
    if not title:
        return False
    return bool(_CITATION_LIKE_RE.search(title)) or len(title) > 120


async def _doi_from_pubmed_url(fetcher: Fetcher, url: str) -> str | None:
    """Resolve a pubmed.ncbi.nlm.nih.gov/<pmid> URL to the article's DOI."""
    m = _PUBMED_URL_RE.search(url)
    if not m:
        return None
    from xml.etree import ElementTree as ET

    try:
        xml = await fetcher.get_text(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": m.group(1)},
        )
        root = ET.fromstring(xml)
        for item in root.findall(".//Item"):
            if item.get("Name") == "doi" and item.text:
                return normalize_doi(item.text)
    except Exception as e:
        log.debug("pubmed url -> doi resolution failed: %s", e)
    return None


def _cr_fields(cr: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _join_first(cr.get("title")),
        "authors": _crossref_authors(cr.get("author") or []) or None,
        "year": _crossref_year(cr),
        "url": cr.get("URL"),
        "journal": _join_first(cr.get("container-title")),
        "publisher": cr.get("publisher"),
    }


def _oa_fields(oa: dict[str, Any]) -> dict[str, Any]:
    authors = [
        a.get("author", {}).get("display_name")
        for a in oa.get("authorships") or []
        if a.get("author", {}).get("display_name")
    ]
    source = ((oa.get("primary_location") or {}).get("source") or {}).get("display_name")
    year = oa.get("publication_year")
    return {
        "title": oa.get("display_name"),
        "authors": authors or None,
        "year": int(year) if isinstance(year, (int, str)) and str(year).isdigit() else None,
        "journal": source,
    }


def _apply(meta: EnrichedMetadata, fields: dict[str, Any], *, override: bool,
           protect_year: int | None, prefer_canonical_title: bool = False) -> None:
    """Merge looked-up fields into `meta`.

    ``override`` (set when the query carried an explicit DOI, so the lookup
    hit the exact record) lets the lookup replace resolver-guessed values;
    otherwise the existing value wins, as before. An explicitly provided
    year always wins over anything looked up. As a courtesy, a looked-up
    title replaces the query title when they match apart from
    casing/whitespace, or when the query "title" is really a pasted
    citation (``prefer_canonical_title``) — a search key, not a title.
    """
    for key, new in fields.items():
        if new is None:
            continue
        if key == "year" and protect_year is not None:
            continue
        old = getattr(meta, key, None)
        if key == "title" and isinstance(new, str) and (
            prefer_canonical_title
            or (isinstance(old, str) and old.strip().lower() == new.strip().lower())
        ):
            setattr(meta, key, new)
        elif override:
            if key == "authors" and not new:
                continue
            setattr(meta, key, new)
        elif not old:
            setattr(meta, key, new)


def _join_first(val: Any) -> str | None:
    if isinstance(val, list):
        return val[0] if val else None
    if isinstance(val, str):
        return val
    return None


def _crossref_authors(authors: list[dict[str, Any]]) -> list[str]:
    out = []
    for a in authors:
        family = a.get("family") or ""
        given = a.get("given") or ""
        full = " ".join(p for p in (given, family) if p).strip()
        if full:
            out.append(full)
    return out


def _crossref_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (ValueError, TypeError):
                continue
    return None
