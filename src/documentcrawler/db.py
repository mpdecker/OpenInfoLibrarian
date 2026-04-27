"""SQLite-backed job queue and attempt log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from documentcrawler.models import (
    AttemptResult,
    DocStatus,
    DocumentQuery,
    DocumentRow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doi         TEXT,
    title       TEXT,
    authors     TEXT,        -- JSON array
    year        INTEGER,
    isbn        TEXT,
    keywords    TEXT,        -- JSON array
    extra       TEXT,        -- JSON object with bibliographic hints
    url         TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    file_path   TEXT,
    sha256      TEXT,
    error       TEXT,
    metadata    TEXT,        -- JSON: enriched metadata
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_doi    ON documents(doi);
-- sha256 is intentionally NOT unique: multiple documents may legitimately
-- share the same file (dedupe path in pipeline points many rows at one PDF).
CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);

CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL,
    source        TEXT NOT NULL,
    candidate_url TEXT,
    http_status   INTEGER,
    bytes         INTEGER,
    success       INTEGER NOT NULL,
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attempts_doc    ON attempts(document_id);
CREATE INDEX IF NOT EXISTS idx_attempts_source ON attempts(source);
"""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        out = json.loads(value)
        return dict(out) if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}
    try:
        out = json.loads(value)
        return list(out) if isinstance(out, list) else []
    except json.JSONDecodeError:
        return []


class Database:
    """Tiny sqlite3 wrapper, no ORM."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Apply small idempotent schema migrations for existing DB files."""
        # Old versions had a UNIQUE partial index on sha256; convert to plain.
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_sha256'"
        ).fetchone()
        if row:
            sql = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_documents_sha256'"
            ).fetchone()
            if sql and sql[0] and "UNIQUE" in sql[0].upper():
                self._conn.execute("DROP INDEX idx_documents_sha256")
                self._conn.execute(
                    "CREATE INDEX idx_documents_sha256 ON documents(sha256)"
                )
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "extra" not in cols:
            self._conn.execute("ALTER TABLE documents ADD COLUMN extra TEXT")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._conn.execute("BEGIN")
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def add_query(self, q: DocumentQuery) -> int:
        if q.is_empty():
            raise ValueError("Refusing to enqueue an empty query (no doi/title/isbn/url).")
        existing = self._find_duplicate(q)
        if existing is not None:
            return existing
        cur = self._conn.execute(
            """
            INSERT INTO documents (doi, title, authors, year, isbn, keywords, extra, url,
                                   status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                q.doi,
                q.title,
                json.dumps(q.authors),
                q.year,
                q.isbn,
                json.dumps(q.keywords),
                json.dumps(q.extra),
                q.url,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        return int(cur.lastrowid)

    def add_many(self, queries: Iterable[DocumentQuery]) -> tuple[int, int]:
        added = 0
        skipped = 0
        for q in queries:
            try:
                before = self._row_count()
                self.add_query(q)
                if self._row_count() == before:
                    skipped += 1
                else:
                    added += 1
            except ValueError:
                skipped += 1
        return added, skipped

    def _row_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def _find_duplicate(self, q: DocumentQuery) -> int | None:
        if q.doi:
            row = self._conn.execute(
                "SELECT id FROM documents WHERE doi = ? COLLATE NOCASE LIMIT 1", (q.doi,)
            ).fetchone()
            if row:
                return int(row["id"])
        if q.url:
            row = self._conn.execute(
                "SELECT id FROM documents WHERE url = ? LIMIT 1", (q.url,)
            ).fetchone()
            if row:
                return int(row["id"])
        if q.title and q.year:
            row = self._conn.execute(
                "SELECT id FROM documents WHERE LOWER(title) = LOWER(?) AND year = ? LIMIT 1",
                (q.title, q.year),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    def get(self, doc_id: int) -> DocumentRow | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return _row_to_document(row) if row else None

    def list_documents(
        self, status: DocStatus | None = None, limit: int | None = None
    ) -> list[DocumentRow]:
        sql = "SELECT * FROM documents"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_document(r) for r in self._conn.execute(sql, params).fetchall()]

    def pending_or_failed(self, only_failed: bool = False) -> list[DocumentRow]:
        if only_failed:
            return self.list_documents(DocStatus.FAILED)
        rows = self._conn.execute(
            "SELECT * FROM documents WHERE status IN ('pending','failed','in_progress') "
            "ORDER BY id ASC"
        ).fetchall()
        return [_row_to_document(r) for r in rows]

    def set_status(
        self,
        doc_id: int,
        status: DocStatus,
        *,
        file_path: str | None = None,
        sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE documents
               SET status = ?, file_path = COALESCE(?, file_path),
                   sha256 = COALESCE(?, sha256), error = ?, updated_at = ?
             WHERE id = ?
            """,
            (status.value, file_path, sha256, error, _utcnow_iso(), doc_id),
        )

    def update_metadata(self, doc_id: int, *, doi: str | None = None,
                        title: str | None = None, authors: list[str] | None = None,
                        year: int | None = None, isbn: str | None = None,
                        url: str | None = None,
                        enriched: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            """
            UPDATE documents
               SET doi      = COALESCE(?, doi),
                   title    = COALESCE(?, title),
                   authors  = COALESCE(?, authors),
                   year     = COALESCE(?, year),
                   isbn     = COALESCE(?, isbn),
                   url      = COALESCE(?, url),
                   metadata = COALESCE(?, metadata),
                   updated_at = ?
             WHERE id = ?
            """,
            (
                doi,
                title,
                json.dumps(authors) if authors is not None else None,
                year,
                isbn,
                url,
                json.dumps(enriched) if enriched is not None else None,
                _utcnow_iso(),
                doc_id,
            ),
        )

    def log_attempt(self, doc_id: int, attempt: AttemptResult) -> None:
        self._conn.execute(
            """
            INSERT INTO attempts (document_id, source, candidate_url, http_status, bytes,
                                  success, error, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                attempt.source,
                attempt.candidate_url,
                attempt.http_status,
                attempt.bytes,
                1 if attempt.success else 0,
                attempt.error,
                attempt.started_at.isoformat(),
                attempt.finished_at.isoformat(),
            ),
        )

    def attempts_for(self, doc_id: int) -> list[AttemptResult]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE document_id = ? ORDER BY id ASC", (doc_id,)
        ).fetchall()
        out: list[AttemptResult] = []
        for r in rows:
            out.append(
                AttemptResult(
                    source=r["source"],
                    success=bool(r["success"]),
                    candidate_url=r["candidate_url"],
                    http_status=r["http_status"],
                    bytes=r["bytes"],
                    error=r["error"],
                    started_at=_parse_dt(r["started_at"]),
                    finished_at=_parse_dt(r["finished_at"]),
                )
            )
        return out

    def status_summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def per_source_stats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT source,
                   SUM(success) AS successes,
                   COUNT(*) - SUM(success) AS failures,
                   COUNT(*) AS total
              FROM attempts
             GROUP BY source
             ORDER BY total DESC
            """
        ).fetchall()
        return [
            {
                "source": r["source"],
                "successes": int(r["successes"] or 0),
                "failures": int(r["failures"] or 0),
                "total": int(r["total"] or 0),
            }
            for r in rows
        ]

    def find_by_sha256(self, sha256: str) -> DocumentRow | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        return _row_to_document(row) if row else None


def _row_to_document(row: sqlite3.Row) -> DocumentRow:
    return DocumentRow(
        id=int(row["id"]),
        doi=row["doi"],
        title=row["title"],
        authors=_json_list(row["authors"]),
        year=row["year"],
        isbn=row["isbn"],
        keywords=_json_list(row["keywords"]),
        extra=_json_object(row["extra"]),
        url=row["url"],
        status=DocStatus(row["status"]),
        file_path=row["file_path"],
        sha256=row["sha256"],
        error=row["error"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )
