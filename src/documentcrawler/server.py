"""HTTP acquisition server for portfolio integration (Zetetic / SecondBrain)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from documentcrawler import __version__
from documentcrawler.config import Config, load_config
from documentcrawler.db import Database
from documentcrawler.errors import DocumentCrawlerError
from documentcrawler.models import DocumentQuery, DocStatus
from documentcrawler.pipeline import Pipeline

log = logging.getLogger("documentcrawler.server")


class AcquireRequest(BaseModel):
    model_config = {"extra": "ignore"}

    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    isbn: str | None = None
    url: str | None = None
    keywords: list[str] = Field(default_factory=list)


class AcquireResponse(BaseModel):
    id: int
    status: str
    doi: str | None = None
    title: str | None = None


class JobResponse(BaseModel):
    id: int
    status: str
    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    isbn: str | None = None
    file_path: str | None = None
    sha256: str | None = None
    error: str | None = None


class QueueSummary(BaseModel):
    total: int
    pending: int
    in_progress: int
    done: int
    failed: int
    items: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    db: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


GRACEFUL_SHUTDOWN_S = 30


def create_app(config_path: Path) -> FastAPI:
    cfg = load_config(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(cfg.general.db_path)
        app.state.config = cfg
        app.state.db = db

        shutdown_event = asyncio.Event()
        app.state._shutdown_event = shutdown_event

        worker_task = asyncio.create_task(_background_worker(app))
        app.state._worker_task = worker_task

        log.info("Server started on %s", cfg.general.db_path)
        try:
            yield
        finally:
            log.info("Server shutting down — signalling worker to drain")
            shutdown_event.set()
            try:
                await asyncio.wait_for(worker_task, timeout=GRACEFUL_SHUTDOWN_S)
            except asyncio.TimeoutError:
                log.warning(
                    "Worker did not finish within %ds; forcing cancel",
                    GRACEFUL_SHUTDOWN_S,
                )
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            db.close()
            log.info("Server stopped")

    app = FastAPI(title="documentcrawler", version=__version__, lifespan=lifespan)

    # --- middleware ----------------------------------------------------------

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Any) -> Any:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000
        log.info(
            "%s %s -> %d (%.1fms)",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
        return response

    # --- exception handlers -------------------------------------------------

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": exc.detail}},
        )

    @app.exception_handler(DocumentCrawlerError)
    async def dc_error_handler(request: Request, exc: DocumentCrawlerError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": type(exc).__name__, "message": str(exc)}},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )

    # --- endpoints -----------------------------------------------------------

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        db: Database = app.state.db
        healthy = db.health()
        return HealthResponse(
            status="ok" if healthy else "degraded",
            db="connected" if healthy else "error",
        )

    @app.post("/acquire", response_model=AcquireResponse)
    async def acquire(body: AcquireRequest, request: Request,
                      timeout: int | None = None) -> AcquireResponse:
        query = DocumentQuery(
            doi=body.doi,
            title=body.title,
            authors=body.authors,
            year=body.year,
            isbn=body.isbn,
            keywords=body.keywords,
            url=body.url,
        )
        if query.is_empty():
            raise HTTPException(status_code=400,
                                detail="No DOI, title, ISBN, or URL provided")

        timeout_s: int | None = None
        if timeout is not None:
            if not (30 <= timeout <= 3600):
                raise HTTPException(status_code=400,
                                    detail="timeout must be between 30 and 3600 seconds")
            timeout_s = timeout

        db: Database = app.state.db
        try:
            doc_id = db.add_query(query, timeout_s=timeout_s)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        doc = db.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=500, detail="Failed to create document")

        return AcquireResponse(
            id=doc.id,
            status=doc.status.value,
            doi=doc.doi,
            title=doc.title,
        )

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: int) -> JobResponse:
        db: Database = app.state.db
        doc = db.get(job_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"No document #{job_id}")
        return JobResponse(
            id=doc.id,
            status=doc.status.value,
            doi=doc.doi,
            title=doc.title,
            authors=doc.authors,
            year=doc.year,
            isbn=doc.isbn,
            file_path=doc.file_path,
            sha256=doc.sha256,
            error=doc.error,
        )

    @app.get("/queue", response_model=QueueSummary)
    async def get_queue() -> QueueSummary:
        db: Database = app.state.db
        counts = db.status_summary()
        docs = db.list_documents(limit=100)
        items: list[dict[str, Any]] = [
            {
                "id": d.id,
                "status": d.status.value,
                "doi": d.doi,
                "title": d.title,
                "year": d.year,
                "file_path": d.file_path,
                "error": d.error,
            }
            for d in docs
        ]
        return QueueSummary(
            total=sum(counts.values()),
            pending=counts.get("pending", 0),
            in_progress=counts.get("in_progress", 0),
            done=counts.get("done", 0),
            failed=counts.get("failed", 0),
            items=items,
        )

    return app


async def _background_worker(app: FastAPI) -> None:
    db: Database = app.state.db
    cfg: Config = app.state.config
    shutdown_event: asyncio.Event = app.state._shutdown_event

    while not shutdown_event.is_set():
        pending = db.list_documents(DocStatus.PENDING)
        if not pending:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue

        doc = pending[0]
        cancel_event = asyncio.Event()

        pipeline = Pipeline(
            cfg, db,
            workers=1,
            cancel_event=cancel_event,
            per_doc_timeout_s=doc.timeout_s or cfg.general.pipeline_timeout_s,
        )

        async def _stop_when_signalled() -> None:
            await shutdown_event.wait()
            cancel_event.set()

        stop_task = asyncio.create_task(_stop_when_signalled())

        try:
            await pipeline.run([doc])
        except asyncio.CancelledError:
            db.set_status(doc.id, DocStatus.FAILED, error="pipeline: cancelled")
            log.warning("Background worker cancelled during doc %d", doc.id)
        except Exception as e:
            log.exception("Background worker pipeline error for doc %d: %s", doc.id, e)
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task


def start_server(host: str, port: int, config_path: Path) -> None:
    import uvicorn

    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
