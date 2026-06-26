"""End-to-end orchestration: enrich -> try sources -> verify -> save -> log."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from documentcrawler.config import Config
from documentcrawler.db import Database
from documentcrawler.errors import AcquisitionError, SourceError
from documentcrawler.fetcher import Fetcher
from documentcrawler.metadata import MetadataEnricher
from documentcrawler.models import (
    AttemptResult,
    Candidate,
    DocStatus,
    DocumentQuery,
    DocumentRow,
    ErrorKind,
)
from documentcrawler.sources import build_source
from documentcrawler.sources.base import Source, SourceContext
from documentcrawler.storage.naming import render_destination
from documentcrawler.storage.writer import (
    InvalidPDFError,
    atomic_write,
    disambiguate,
    sha256_of,
    verify_pdf,
)
from documentcrawler.utils.logging import get_logger

ProgressCallback = Callable[[str, dict[str, Any]], None]

log = get_logger(__name__)


@dataclass
class PipelineSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_already_done: int = 0
    per_source: dict[str, int] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        sources_override: list[str] | None = None,
        legit_only: bool = False,
        workers: int = 4,
        progress_cb: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        per_doc_timeout_s: float | None = None,
        dry_run: bool = False,
    ):
        self.config = config
        self.db = db
        self.workers = workers
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.per_doc_timeout_s = per_doc_timeout_s
        self.dry_run = dry_run

        if legit_only:
            sources_override = ["open_access", "arxiv", "pubmed", "doaj"]

        names = config.enabled_sources_in_order(sources_override)
        self._source_instances: list[Source] = []
        for name in names:
            cfg = config.source(name)
            inst = build_source(name, cfg)
            if inst is None:
                log.warning("Unknown source %r in config order; skipping", name)
                continue
            self._source_instances.append(inst)

        if not self._source_instances:
            log.warning("No sources are enabled. Edit config.toml [sources].order.")

    async def run(self, docs: list[DocumentRow]) -> PipelineSummary:
        summary = PipelineSummary(total=len(docs))
        if not docs:
            return summary

        self._emit("run_start", {"total": len(docs)})

        async with Fetcher(
            self.config.fetcher,
            timeout_s=self.config.general.request_timeout_s,
            max_retries=self.config.general.max_retries,
        ) as fetcher:
            enricher = self._build_enricher(fetcher)

            # Use a rich progress bar only when no external progress_cb is set
            # (i.e. the CLI is the user). GUI consumers pass progress_cb and
            # render their own progress bar.
            progress_ctx: Any
            if self.progress_cb is None:
                progress_ctx = Progress(
                    SpinnerColumn(),
                    TextColumn("[bold]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    TimeElapsedColumn(),
                    transient=False,
                )
            else:
                progress_ctx = nullcontext()

            with progress_ctx as progress:
                task_id = (
                    progress.add_task("Downloading", total=len(docs))
                    if isinstance(progress, Progress)
                    else None
                )
                sem = asyncio.Semaphore(max(1, self.workers))

                async def worker(doc: DocumentRow) -> None:
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        return
                    async with sem:
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            return
                        self._emit("doc_start", {"id": doc.id, "title": doc.title,
                                                  "doi": doc.doi})
                        try:
                            ok, used_source = await self._process_one(doc, fetcher, enricher)
                        except asyncio.CancelledError:
                            self.db.set_status(doc.id, DocStatus.FAILED,
                                               error="pipeline: cancelled")
                            ok, used_source = False, None
                        except Exception as e:
                            log.exception("Pipeline error on doc %d: %s", doc.id, e)
                            ok, used_source = False, None
                        if ok:
                            summary.succeeded += 1
                            if used_source:
                                summary.per_source[used_source] = (
                                    summary.per_source.get(used_source, 0) + 1
                                )
                        else:
                            summary.failed += 1
                        self._emit(
                            "doc_done",
                            {
                                "id": doc.id,
                                "ok": ok,
                                "source": used_source,
                                "completed": summary.succeeded + summary.failed,
                                "total": summary.total,
                            },
                        )
                        if isinstance(progress, Progress) and task_id is not None:
                            progress.advance(task_id)

                await asyncio.gather(
                    *(worker(doc) for doc in docs), return_exceptions=True
                )

        self._emit("run_done", {
            "total": summary.total,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "per_source": dict(summary.per_source),
        })
        return summary

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress_cb is None:
            return
        try:
            self.progress_cb(event, payload)
        except Exception as e:
            log.debug("progress_cb error: %s", e)

    def _build_enricher(self, fetcher: Fetcher) -> MetadataEnricher:
        unpaywall_email = (self.config.metadata.get("unpaywall") or {}).get("email")
        crossref_mailto = (self.config.metadata.get("crossref") or {}).get("mailto")
        openalex_mailto = (self.config.metadata.get("openalex") or {}).get("mailto")
        semantic_scholar_api_key = (
            (self.config.metadata.get("semantic_scholar") or {}).get("api_key")
        )
        if unpaywall_email == "you@example.com":
            unpaywall_email = None
        return MetadataEnricher(
            fetcher,
            unpaywall_email=unpaywall_email,
            crossref_mailto=crossref_mailto,
            openalex_mailto=openalex_mailto,
            semantic_scholar_api_key=semantic_scholar_api_key,
        )

    async def _process_one(
        self,
        doc: DocumentRow,
        fetcher: Fetcher,
        enricher: MetadataEnricher,
    ) -> tuple[bool, str | None]:
        if doc.status == DocStatus.DONE and doc.file_path:
            return True, None

        self.db.set_status(doc.id, DocStatus.IN_PROGRESS)

        query = DocumentQuery(
            doi=doc.doi, title=doc.title, authors=doc.authors, year=doc.year,
            isbn=doc.isbn, keywords=doc.keywords, extra=dict(doc.extra), url=doc.url,
        )

        async def _inner() -> tuple[bool, str | None]:
            nonlocal doc
            try:
                metadata = await enricher.enrich(query)
            except Exception as e:
                log.warning("Metadata enrichment failed for #%d: %s", doc.id, e)
                metadata = await self._fallback_metadata(query, fetcher, enricher)

            self.db.update_metadata(
                doc.id,
                doi=metadata.doi,
                title=metadata.title,
                authors=metadata.authors or None,
                year=metadata.year,
                isbn=metadata.isbn,
                url=metadata.url,
                enriched={
                    "oa_urls": metadata.oa_urls,
                    "pmid": metadata.pmid,
                    "pmcid": metadata.pmcid,
                    "journal": metadata.journal,
                    "publisher": metadata.publisher,
                },
            )
            doc = self.db.get(doc.id) or doc

            last_error: str | None = None
            for source in self._source_instances:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    return False, None

                cfg = self.config.source(source.name)
                ctx = SourceContext(query=query, metadata=metadata, fetcher=fetcher,
                                    options=cfg.options)
                candidates: list[Candidate] = []
                try:
                    candidates = await source.search(ctx)
                except Exception as e:
                    log.debug("source %s search error: %s", source.name, e)
                    self._log_failure(doc.id, source.name, None,
                                      f"search: {e}", error_kind=ErrorKind.TRANSIENT.value)
                    last_error = f"{source.name}: search failed"
                    continue

                if not candidates:
                    continue

                if self.dry_run:
                    urls = [c.url for c in candidates[:3]]
                    log.info(
                        "dry-run doc #%d: source %s found %d candidate(s): %s",
                        doc.id, source.name, len(candidates), urls,
                    )
                    self._log_failure(doc.id, source.name,
                                      candidates[0].url if candidates else None,
                                      f"dry-run: would download from {source.name} "
                                      f"({len(candidates)} candidate(s))",
                                      error_kind=ErrorKind.TRANSIENT.value)
                    continue

                for candidate in candidates:
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        return False, None

                    started = _utcnow()
                    try:
                        data = await source.fetch(candidate, ctx)
                    except Exception as e:
                        self._log_failure(doc.id, source.name, candidate.url,
                                          f"fetch: {e}", started_at=started,
                                          error_kind=ErrorKind.TRANSIENT.value)
                        last_error = f"{source.name}: {e}"
                        continue

                    if not data:
                        self._log_failure(doc.id, source.name, candidate.url,
                                          "empty response", started_at=started,
                                          error_kind=ErrorKind.TRANSIENT.value)
                        continue

                    try:
                        verify_pdf(data, min_bytes=self.config.general.min_pdf_bytes)
                    except InvalidPDFError as e:
                        self._log_failure(doc.id, source.name, candidate.url,
                                          f"verify: {e}", started_at=started,
                                          bytes_=len(data), error_kind=ErrorKind.PERMANENT.value)
                        last_error = f"{source.name}: not a pdf"
                        continue

                    sha = sha256_of(data)
                    existing = self.db.find_by_sha256(sha)
                    if existing and existing.id != doc.id and existing.file_path:
                        self.db.set_status(
                            doc.id,
                            DocStatus.DONE,
                            file_path=existing.file_path,
                            sha256=sha,
                            error=None,
                        )
                        self.db.log_attempt(
                            doc.id,
                            AttemptResult(
                                source=source.name,
                                success=True,
                                candidate_url=candidate.url,
                                bytes=len(data),
                                started_at=started,
                                finished_at=_utcnow(),
                            ),
                        )
                        return True, source.name

                    dest = render_destination(
                        self.config.general.download_dir,
                        doc,
                        filename_template=self.config.general.filename_template,
                        folder_template=self.config.general.folder_template,
                        ext="pdf",
                    )
                    dest = disambiguate(dest)
                    try:
                        atomic_write(dest, data)
                    except OSError as e:
                        self._log_failure(doc.id, source.name, candidate.url,
                                          f"write: {e}", started_at=started,
                                          bytes_=len(data), error_kind=ErrorKind.PERMANENT.value)
                        last_error = f"{source.name}: write error"
                        continue

                    self.db.set_status(
                        doc.id, DocStatus.DONE, file_path=str(dest), sha256=sha,
                        error=None
                    )
                    self.db.log_attempt(
                        doc.id,
                        AttemptResult(
                            source=source.name,
                            success=True,
                            candidate_url=candidate.url,
                            bytes=len(data),
                            started_at=started,
                            finished_at=_utcnow(),
                        ),
                    )
                    return True, source.name

            if self.dry_run:
                self.db.set_status(doc.id, DocStatus.PENDING, error=None)
            else:
                self.db.set_status(doc.id, DocStatus.FAILED,
                                   error=last_error or "no source produced a PDF")
            return False, None

        if self.per_doc_timeout_s is not None and self.per_doc_timeout_s > 0:
            try:
                return await asyncio.wait_for(_inner(), timeout=self.per_doc_timeout_s)
            except asyncio.TimeoutError:
                self.db.set_status(
                    doc.id, DocStatus.FAILED,
                    error=f"pipeline: timeout after {self.per_doc_timeout_s:.0f}s"
                )
                self._log_failure(doc.id, "pipeline", None,
                                  f"timeout after {self.per_doc_timeout_s:.0f}s",
                                  error_kind=ErrorKind.TRANSIENT.value)
                return False, None
            except asyncio.CancelledError:
                self.db.set_status(doc.id, DocStatus.FAILED, error="pipeline: cancelled")
                raise
        else:
            try:
                return await _inner()
            except asyncio.CancelledError:
                self.db.set_status(doc.id, DocStatus.FAILED, error="pipeline: cancelled")
                raise

    async def _fallback_metadata(self, query: DocumentQuery, fetcher: Fetcher,
                                 enricher: MetadataEnricher):
        # Provide an empty enrichment if Crossref/OpenAlex are completely unreachable.
        from documentcrawler.metadata.enricher import EnrichedMetadata
        return EnrichedMetadata(
            doi=query.doi, title=query.title, authors=list(query.authors),
            year=query.year, isbn=query.isbn, url=query.url,
        )

    def _log_failure(
        self,
        doc_id: int,
        source: str,
        url: str | None,
        error: str,
        *,
        started_at: datetime | None = None,
        bytes_: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        now = _utcnow()
        self.db.log_attempt(
            doc_id,
            AttemptResult(
                source=source,
                success=False,
                candidate_url=url,
                bytes=bytes_,
                error=error[:500],
                error_kind=error_kind,
                started_at=started_at or now,
                finished_at=now,
            ),
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)
