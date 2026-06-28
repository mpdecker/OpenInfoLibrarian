"""SQLite-backed job queue and attempt log."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from documentcrawler.errors import ConfigError
from documentcrawler.models import (
    AttemptResult,
    DocStatus,
    DocumentQuery,
    DocumentRow,
    SavedSearchRow,
)

class _JSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, set):
            return sorted(list(o))
        return super().default(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, cls=_JSONEncoder)


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

CREATE TABLE IF NOT EXISTS saved_searches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    query_text        TEXT NOT NULL,
    kind              TEXT DEFAULT 'auto',
    sources           TEXT,        -- JSON array
    limit_per_source  INTEGER DEFAULT 15,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_saved_searches_name ON saved_searches(name);
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
    try:
        out = json.loads(value)
        return list(out) if isinstance(out, list) else []
    except json.JSONDecodeError:
        return []


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        out = json.loads(value)
        return dict(out) if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


def _first_author_surname_for_db(authors: list[str]) -> str:
    if not authors:
        return ""
    name = authors[0].strip()
    if "," in name:
        return name.split(",")[0].strip().lower()
    parts = name.split()
    return parts[-1].lower() if parts else name.lower()


def _safe_int(row: sqlite3.Row, key: str) -> int | None:
    try:
        val = row[key]
    except (KeyError, IndexError):
        return None
    if val is None:
        return None
    return int(val)


class Database:
    """Tiny sqlite3 wrapper, no ORM."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._validate_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(SCHEMA)
        self._run_versioned_migrations()

    def _validate_path(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ConfigError(
                    f"Cannot create database directory {parent}: {e}"
                ) from e
        if parent.exists() and not os.access(parent, os.W_OK):
            raise ConfigError(
                f"Database directory {parent} is not writable"
            )

    def _run_versioned_migrations(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        current = self._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0] or 0

        migrations = [
            (1, self._migrate_1),
            (2, self._migrate_2),
            (3, self._migrate_3),
            (4, self._migrate_4),
        ]

        for version, fn in migrations:
            if version > current:
                fn()
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )

    def _migrate_1(self) -> None:
        """Add `extra` column to documents if missing."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "extra" not in cols:
            self._conn.execute("ALTER TABLE documents ADD COLUMN extra TEXT")

    def _migrate_2(self) -> None:
        """Convert old UNIQUE partial index on sha256 to plain index."""
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

    def _migrate_3(self) -> None:
        """Add error_kind column to attempts table."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if "error_kind" not in cols:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN error_kind TEXT")

    def _migrate_4(self) -> None:
        """Add timeout_s column to documents table."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "timeout_s" not in cols:
            self._conn.execute("ALTER TABLE documents ADD COLUMN timeout_s INTEGER")

    def close(self) -> None:
        self._conn.close()

    def health(self) -> bool:
        try:
            self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._conn.execute("BEGIN")
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    @contextmanager
    def retry_transaction(self, max_attempts: int = 3) -> Iterator[sqlite3.Connection]:
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                with self.transaction() as conn:
                    yield conn
                return
            except sqlite3.OperationalError as e:
                last_err = e
                if attempt < max_attempts:
                    time.sleep(0.05 * attempt)
            except Exception:
                raise
        raise last_err  # type: ignore[misc]

    def add_query(self, q: DocumentQuery, timeout_s: int | None = None) -> int:
        if q.is_empty():
            raise ValueError("Refusing to enqueue an empty query (no doi/title/isbn/url).")
        existing = self._find_duplicate(q)
        if existing is not None:
            return existing
        cur = self._conn.execute(
            """
            INSERT INTO documents (doi, title, authors, year, isbn, keywords, extra, url,
                                   timeout_s, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                q.doi,
                q.title,
                json.dumps(q.authors),
                q.year,
                q.isbn,
                json.dumps(q.keywords),
                _dumps(q.extra),
                q.url,
                timeout_s,
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
        if q.title and q.authors:
            author_surname = _first_author_surname_for_db(q.authors)
            if author_surname:
                row = self._conn.execute(
                    """SELECT d.id FROM documents d, json_each(d.authors)
                       WHERE LOWER(d.title) = LOWER(?)
                       AND LOWER(json_each.value) LIKE ?
                       LIMIT 1""",
                    (q.title, f"%{author_surname}%"),
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

    def pending_or_failed(
        self, only_failed: bool = False, retry_permanent: bool = False
    ) -> list[DocumentRow]:
        if only_failed:
            if retry_permanent:
                return self.list_documents(DocStatus.FAILED)
            return self._transient_failures()
        rows = self._conn.execute(
            "SELECT * FROM documents WHERE status IN ('pending','failed','in_progress') "
            "ORDER BY id ASC"
        ).fetchall()
        return [_row_to_document(r) for r in rows]

    def _transient_failures(self) -> list[DocumentRow]:
        rows = self._conn.execute(
            """SELECT d.* FROM documents d
               WHERE d.status = 'failed'
               AND COALESCE(
                 (SELECT a.error_kind FROM attempts a
                  WHERE a.document_id = d.id
                  ORDER BY a.id DESC LIMIT 1),
                 'transient'
               ) = 'transient'
               ORDER BY d.id ASC"""
        ).fetchall()
        return [_row_to_document(r) for r in rows]

    def failed_by_source(self, source: str) -> list[DocumentRow]:
        rows = self._conn.execute(
            """SELECT d.* FROM documents d
               WHERE d.status = 'failed'
               AND (SELECT a.source FROM attempts a
                    WHERE a.document_id = d.id
                    ORDER BY a.id DESC LIMIT 1) = ?
               ORDER BY d.id ASC""",
            (source,)
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
                _dumps(enriched) if enriched is not None else None,
                _utcnow_iso(),
                doc_id,
            ),
        )

    def log_attempt(self, doc_id: int, attempt: AttemptResult) -> None:
        self._conn.execute(
            """
            INSERT INTO attempts (document_id, source, candidate_url, http_status, bytes,
                                  success, error, error_kind, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                attempt.source,
                attempt.candidate_url,
                attempt.http_status,
                attempt.bytes,
                1 if attempt.success else 0,
                attempt.error,
                attempt.error_kind,
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
                    error_kind=r["error_kind"] if "error_kind" in r.keys() else None,  # noqa: SIM401
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

    # -------------------------------------------------------------------------
    # Saved searches
    # -------------------------------------------------------------------------

    def save_search(
        self,
        name: str,
        query_text: str,
        kind: str = "auto",
        sources: list[str] | None = None,
        limit_per_source: int = 15,
    ) -> int:
        """Save a search configuration. Returns the new saved search id."""
        cur = self._conn.execute(
            """
            INSERT INTO saved_searches (name, query_text, kind, sources, limit_per_source,
                                        created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                query_text,
                kind,
                json.dumps(sources or []),
                limit_per_source,
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        return int(cur.lastrowid)

    def list_saved_searches(self, limit: int = 50) -> list[SavedSearchRow]:
        """Return saved searches ordered by most recently updated first."""
        rows = self._conn.execute(
            """
            SELECT * FROM saved_searches
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_saved_search(r) for r in rows]

    def get_saved_search(self, search_id: int) -> SavedSearchRow | None:
        """Get a single saved search by id."""
        row = self._conn.execute(
            "SELECT * FROM saved_searches WHERE id = ?", (search_id,)
        ).fetchone()
        return _row_to_saved_search(row) if row else None

    def delete_saved_search(self, search_id: int) -> None:
        """Delete a saved search by id."""
        self._conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))

    def update_saved_search(
        self,
        search_id: int,
        *,
        name: str | None = None,
        query_text: str | None = None,
        kind: str | None = None,
        sources: list[str] | None = None,
        limit_per_source: int | None = None,
    ) -> None:
        """Update a saved search. Only provided fields are updated."""
        current = self.get_saved_search(search_id)
        if current is None:
            raise ValueError(f"Saved search {search_id} not found")
        self._conn.execute(
            """
            UPDATE saved_searches
               SET name = COALESCE(?, name),
                   query_text = COALESCE(?, query_text),
                   kind = COALESCE(?, kind),
                   sources = COALESCE(?, sources),
                   limit_per_source = COALESCE(?, limit_per_source),
                   updated_at = ?
             WHERE id = ?
            """,
            (
                name,
                query_text,
                kind,
                json.dumps(sources) if sources is not None else None,
                limit_per_source,
                _utcnow_iso(),
                search_id,
            ),
        )

    def get_autocomplete_suggestions(self, prefix: str, limit: int = 10) -> list[str]:
        """Return autocomplete suggestions from saved searches and document titles."""
        suggestions: list[str] = []
        seen: set[str] = set()

        # First: saved searches (most recent first)
        rows = self._conn.execute(
            """
            SELECT query_text FROM saved_searches
            WHERE query_text LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (f"%{prefix}%", limit),
        ).fetchall()
        for r in rows:
            text = r["query_text"]
            if text and text.lower() not in seen:
                suggestions.append(text)
                seen.add(text.lower())

        # Then: document titles (distinct, non-pending)
        remaining = limit - len(suggestions)
        if remaining > 0:
            rows = self._conn.execute(
                """
                SELECT DISTINCT title FROM documents
                WHERE title LIKE ? AND status != 'pending'
                ORDER BY title ASC
                LIMIT ?
                """,
                (f"%{prefix}%", remaining),
            ).fetchall()
            for r in rows:
                text = r["title"]
                if text and text.lower() not in seen:
                    suggestions.append(text)
                    seen.add(text.lower())

        return suggestions


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
        timeout_s=_safe_int(row, "timeout_s"),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_saved_search(row: sqlite3.Row) -> SavedSearchRow:
    return SavedSearchRow(
        id=int(row["id"]),
        name=row["name"],
        query_text=row["query_text"],
        kind=row["kind"] or "auto",
        sources=_json_list(row["sources"]),
        limit_per_source=row["limit_per_source"] or 15,
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )
