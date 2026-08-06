"""High-level metadata enrichment combining Crossref / OpenAlex / Unpaywall."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from documentcrawler.fetcher import Fetcher
from documentcrawler.metadata.arxiv import arxiv_lookup, extract_arxiv_id
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
    raw: dict[str, Any] = field(default_factory=dict)


class MetadataEnricher:
    def __init__(self, fetcher: Fetcher, *, unpaywall_email: str | None = None,
                 crossref_mailto: str | None = None,
                 openalex_mailto: str | None = None,
                 semantic_scholar_api_key: str | None = None):
        self.fetcher = fetcher
        self.unpaywall_email = unpaywall_email
        self.crossref_mailto = crossref_mailto
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

        if meta.doi:
            tasks = [
                crossref_lookup(self.fetcher, meta.doi, mailto=self.crossref_mailto),
                openalex_lookup(self.fetcher, meta.doi),
            ]
            if self.unpaywall_email:
                tasks.append(unpaywall_lookup(self.fetcher, meta.doi, self.unpaywall_email))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            cr = results[0] if len(results) > 0 and isinstance(results[0], dict) else None
            oa = results[1] if len(results) > 1 and isinstance(results[1], dict) else None
            up = results[2] if len(results) > 2 and isinstance(results[2], dict) else None

            if cr:
                meta.title = meta.title or _join_first(cr.get("title"))
                if not meta.authors:
                    meta.authors = _crossref_authors(cr.get("author") or [])
                meta.year = meta.year or _crossref_year(cr)
                meta.url = meta.url or cr.get("URL")
                meta.journal = meta.journal or _join_first(cr.get("container-title"))
                meta.publisher = meta.publisher or cr.get("publisher")
                meta.raw["crossref"] = cr

            if oa:
                meta.raw["openalex"] = oa
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

        arxiv_id = extract_arxiv_id(q.url) or extract_arxiv_id(q.title)
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
