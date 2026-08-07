"""SQLite-backed job queue and attempt log."""

from __future__ import annotations

import contextlib
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
        if isinstance(o, datetime):
            return o.isoformat()
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
    priority    INTEGER NOT NULL DEFAULT 0,
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
    error_kind    TEXT,
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
        self._conn.execute("PRAGMA synchronous = NORMAL")
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
            (5, self._migrate_5),
            (6, self._migrate_6),
            (7, self._migrate_7),
        ]

        for version, fn in migrations:
            if version > current:
                fn()
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )

    def _migrate_7(self) -> None:
        """Add webhooks table."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                events TEXT NOT NULL DEFAULT '*',
                secret TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
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

    def _migrate_5(self) -> None:
        """Add FTS5 virtual table for full-text search across documents."""
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5("
                "title, authors, keywords, doi, isbn, content='documents', content_rowid='id')"
            )
            # Sync existing documents into FTS5 index
            self._conn.execute(
                "INSERT INTO documents_fts(rowid, title, authors, keywords, doi, isbn) "
                "SELECT id, COALESCE(title, ''), COALESCE(authors, ''), COALESCE(keywords, ''), "
                "COALESCE(doi, ''), COALESCE(isbn, '') FROM documents"
            )
            # Add triggers to keep FTS index synchronized
            self._conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_documents_ai AFTER INSERT ON documents BEGIN
                    INSERT INTO documents_fts(rowid, title, authors, keywords, doi, isbn)
                    VALUES (new.id, COALESCE(new.title, ''), COALESCE(new.authors, ''),
                            COALESCE(new.keywords, ''), COALESCE(new.doi, ''), COALESCE(new.isbn, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS trg_documents_ad AFTER DELETE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, title, authors, keywords, doi, isbn)
                    VALUES('delete', old.id, COALESCE(old.title, ''), COALESCE(old.authors, ''),
                           COALESCE(old.keywords, ''), COALESCE(old.doi, ''), COALESCE(old.isbn, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS trg_documents_au AFTER UPDATE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, title, authors, keywords, doi, isbn)
                    VALUES('delete', old.id, COALESCE(old.title, ''), COALESCE(old.authors, ''),
                           COALESCE(old.keywords, ''), COALESCE(old.doi, ''), COALESCE(old.isbn, ''));
                    INSERT INTO documents_fts(rowid, title, authors, keywords, doi, isbn)
                    VALUES (new.id, COALESCE(new.title, ''), COALESCE(new.authors, ''),
                            COALESCE(new.keywords, ''), COALESCE(new.doi, ''), COALESCE(new.isbn, ''));
                END;
                """
            )

    def _migrate_6(self) -> None:
        """Add priority column to documents table."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "priority" not in cols:
            self._conn.execute("ALTER TABLE documents ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_priority ON documents(priority)")

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self._conn.close()

    def health(self) -> bool:
        try:
            self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def vacuum(self) -> None:
        """Reclaim unused database space and optimize indices."""
        self._conn.execute("VACUUM")
        self._conn.execute("PRAGMA optimize")

    def integrity_check(self) -> bool:
        """Run SQLite integrity check."""
        try:
            row = self._conn.execute("PRAGMA quick_check").fetchone()
            return row is not None and row[0].lower() == "ok"
        except sqlite3.Error:
            return False

    def backup(self, dest_path: Path) -> None:
        """Perform a live thread-safe hot backup of the SQLite database."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_conn = sqlite3.connect(dest_path)
        try:
            with dest_conn:
                self._conn.backup(dest_conn)
        finally:
            dest_conn.close()

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
                                   timeout_s, priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                q.priority,
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
        sql += " ORDER BY priority DESC, id ASC"
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
            "ORDER BY priority DESC, id ASC"
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
               ORDER BY d.priority DESC, d.id ASC"""
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

    def get_source_diagnostics(self) -> list[dict[str, Any]]:
        """Return diagnostic metrics per source calculated from attempt logs."""
        rows = self._conn.execute(
            """
            SELECT
                source,
                COUNT(*) as total_attempts,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_attempts,
                COALESCE(SUM(bytes), 0) as total_bytes,
                SUM(CASE WHEN error_kind = 'transient' THEN 1 ELSE 0 END) as transient_errors,
                SUM(CASE WHEN error_kind = 'permanent' THEN 1 ELSE 0 END) as permanent_errors
            FROM attempts
            GROUP BY source
            ORDER BY total_attempts DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            tot = r["total_attempts"]
            succ = r["successful_attempts"]
            out.append({
                "source": r["source"],
                "total_attempts": tot,
                "successful_attempts": succ,
                "success_rate": round((succ / tot) * 100, 1) if tot > 0 else 0.0,
                "total_bytes": r["total_bytes"],
                "transient_errors": r["transient_errors"],
                "permanent_errors": r["permanent_errors"],
            })
        return out

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

    def search_fts(self, query: str, limit: int = 50) -> list[DocumentRow]:
        """Full-text search across documents using FTS5 virtual table."""
        if not query.strip():
            return []
        try:
            cur = self._conn.execute(
                "SELECT d.* FROM documents d JOIN documents_fts fts ON d.id = fts.rowid "
                "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            )
            return [_row_to_document(r) for r in cur.fetchall()]
        except sqlite3.Error:
            # Fallback to standard LIKE if FTS table or query syntax is unavailable
            pattern = f"%{query.strip()}%"
            cur = self._conn.execute(
                "SELECT * FROM documents WHERE title LIKE ? OR authors LIKE ? OR keywords LIKE ? "
                "OR doi LIKE ? OR isbn LIKE ? ORDER BY id DESC LIMIT ?",
                (pattern, pattern, pattern, pattern, pattern, limit),
            )
            return [_row_to_document(r) for r in cur.fetchall()]

    def optimize_fts(self) -> None:
        """Optimize SQLite FTS5 index structure to merge B-tree segments."""
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute(
                "INSERT INTO documents_fts(documents_fts) VALUES('optimize')"
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
                    error_kind=r["error_kind"] if "error_kind" in r.keys() else None,  # noqa: SIM118, SIM401
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

    # -------------------------------------------------------------------------
    # Webhooks & Diagnostics
    # -------------------------------------------------------------------------

    def add_webhook(self, url: str, events: str = "*", secret: str | None = None) -> int:
        """Register a new webhook URL."""
        cur = self._conn.execute(
            """
            INSERT INTO webhooks (url, events, secret, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (url, events, secret, _utcnow_iso()),
        )
        return int(cur.lastrowid)

    def list_webhooks(self) -> list[dict[str, Any]]:
        """List registered webhooks."""
        rows = self._conn.execute(
            "SELECT * FROM webhooks ORDER BY id ASC"
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "url": r["url"],
                "events": r["events"],
                "secret": r["secret"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def delete_webhook(self, webhook_id: int) -> bool:
        """Delete a registered webhook by ID."""
        cur = self._conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cur.rowcount > 0

    def get_db_stats(self) -> dict[str, Any]:
        """Return comprehensive DB metrics (file size, document counts, table row counts)."""
        file_bytes = self.path.stat().st_size if self.path.exists() else 0
        doc_counts = self.status_summary()
        attempts_count = int(self._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        webhooks_count = int(self._conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0])
        integrity = self.integrity_check()
        return {
            "path": str(self.path),
            "size_bytes": file_bytes,
            "size_mb": round(file_bytes / (1024 * 1024), 2),
            "integrity_ok": integrity,
            "documents": doc_counts,
            "total_documents": sum(doc_counts.values()),
            "total_attempts": attempts_count,
            "total_webhooks": webhooks_count,
        }

    def merge_documents(self, primary_id: int, secondary_ids: list[int]) -> DocumentRow:
        """Consolidate metadata and attempt history from secondary_ids into primary_id and delete secondary records."""
        primary = self.get(primary_id)
        if primary is None:
            raise ValueError(f"Primary document #{primary_id} not found")

        secondaries = [self.get(sid) for sid in secondary_ids if sid != primary_id]
        secondaries = [s for s in secondaries if s is not None]

        if not secondaries:
            return primary

        # Consolidate metadata
        doi = primary.doi or next((s.doi for s in secondaries if s.doi), None)
        title = primary.title or next((s.title for s in secondaries if s.title), None)
        authors = list(dict.fromkeys(primary.authors + [a for s in secondaries for a in s.authors]))
        year = primary.year or next((s.year for s in secondaries if s.year), None)
        isbn = primary.isbn or next((s.isbn for s in secondaries if s.isbn), None)
        url = primary.url or next((s.url for s in secondaries if s.url), None)
        keywords = list(dict.fromkeys(primary.keywords + [k for s in secondaries for k in s.keywords]))
        file_path = primary.file_path or next((s.file_path for s in secondaries if s.file_path), None)
        sha256 = primary.sha256 or next((s.sha256 for s in secondaries if s.sha256), None)

        status = primary.status
        if status != DocStatus.DONE:
            for s in secondaries:
                if s.status == DocStatus.DONE:
                    status = DocStatus.DONE
                    break

        with self.transaction():
            self.update_metadata(
                primary_id,
                doi=doi,
                title=title,
                authors=authors,
                year=year,
                isbn=isbn,
                url=url,
            )
            self.set_status(primary_id, status, file_path=file_path, sha256=sha256)

            for sid in secondary_ids:
                if sid != primary_id:
                    self._conn.execute(
                        "UPDATE attempts SET document_id = ? WHERE document_id = ?",
                        (primary_id, sid),
                    )
                    self._conn.execute("DELETE FROM documents WHERE id = ?", (sid,))

        return self.get(primary_id) or primary

    def clean_metadata(self) -> int:
        """Scan and sanitize document titles, author lists, and keywords across the database."""
        from documentcrawler.cleaner import clean_document_metadata

        docs = self.list_documents(limit=10000)
        cleaned_count = 0
        with self.transaction():
            for doc in docs:
                cleaned = clean_document_metadata(doc)
                if (
                    cleaned["title"] != doc.title
                    or cleaned["authors"] != doc.authors
                    or cleaned["keywords"] != doc.keywords
                ):
                    self._conn.execute(
                        """
                        UPDATE documents
                           SET title = ?, authors = ?, keywords = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            cleaned["title"],
                            json.dumps(cleaned["authors"]),
                            json.dumps(cleaned["keywords"]),
                            _utcnow_iso(),
                            doc.id,
                        ),
                    )
                    cleaned_count += 1
        return cleaned_count


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
        priority=_safe_int(row, "priority") or 0,
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
