"""Tkinter desktop GUI for documentcrawler.

Launch with `documentcrawler gui`. The GUI reads/writes the same config.toml
as the CLI and shares the SQLite database, so the two interfaces are fully
interoperable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

# `documentcrawler.searcher` is imported eagerly so its sub-modules register.
import documentcrawler.searcher  # noqa: F401
from documentcrawler import __version__
from documentcrawler.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    SourceConfig,
    load_config,
    write_default_config,
)
from documentcrawler.config_writer import write_config
from documentcrawler.db import Database
from documentcrawler.fetcher import Fetcher
from documentcrawler.importers import parse_file
from documentcrawler.models import DocStatus, DocumentQuery, DocumentRow
from documentcrawler.pipeline import Pipeline
from documentcrawler.presentation import (
    copyable_document_url,
    NormalizedDocumentRow,
    NormalizedSearchHit,
    normalize_document_row,
    normalize_search_hit,
    preferred_queue_url,
)
from documentcrawler.searcher import (
    MultiSearcher,
    SearchHit,
    SearchQuery,
)
from documentcrawler.searcher.aggregate import SearchResult
from documentcrawler.utils.logging import get_logger
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

log = get_logger("gui")

# Cap the GUI log buffer so a long run doesn't make Tk grind.
_LOG_MAX_LINES = 2000

_SHORTCUTS: list[tuple[str, str]] = [
    ("Ctrl+N",        "Add document"),
    ("Ctrl+O",        "Import file"),
    ("Ctrl+F",        "Focus filter"),
    ("Ctrl+K",        "Open search"),
    ("Ctrl+R / F5",   "Run pipeline"),
    ("Ctrl+. / Esc",  "Cancel run / clear filter"),
    ("Ctrl+E",        "Edit metadata of selected"),
    ("Ctrl+,",        "Settings"),
    ("Enter",         "Open file (in queue) / search (in search dialog)"),
    ("Delete",        "Delete from queue (with confirm)"),
    ("F1",            "Help / shortcuts"),
]

ALL_SOURCES: list[tuple[str, str]] = [
    ("open_access", "Open access (Unpaywall / OpenAlex direct PDF)"),
    ("arxiv", "arXiv"),
    ("pubmed", "PubMed Central"),
    ("doaj", "DOAJ"),
    ("scihub", "Sci-Hub  (shadow library)"),
    ("annas_archive", "Anna's Archive  (shadow library)"),
    ("libgen", "LibGen  (shadow library)"),
    ("zlibrary", "Z-Library  (shadow library, Playwright)"),
]

SHADOW_SOURCES = {"scihub", "annas_archive", "libgen", "zlibrary"}

ALL_SEARCHERS: list[tuple[str, str, bool]] = [
    # (name, label, is_shadow)
    ("crossref", "Crossref", False),
    ("openalex", "OpenAlex", False),
    ("arxiv", "arXiv", False),
    ("openlibrary", "Open Library", False),
    ("semantic_scholar", "Semantic Scholar", False),
    ("annas_archive", "Anna's Archive", True),
    ("libgen", "LibGen", True),
    ("zlibrary", "Z-Library", True),
]
SHADOW_SEARCHERS = {n for n, _, sh in ALL_SEARCHERS if sh}

STATUS_FILTERS = [
    ("All", None),
    ("Pending", DocStatus.PENDING),
    ("In progress", DocStatus.IN_PROGRESS),
    ("Done", DocStatus.DONE),
    ("Failed", DocStatus.FAILED),
]


# -----------------------------------------------------------------------------
# Worker thread that runs the asyncio pipeline
# -----------------------------------------------------------------------------


class PipelineWorker:
    """Run Pipeline.run on a background thread, push events into a Queue."""

    def __init__(self, app: App):
        self.app = app
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.cancel_event: asyncio.Event | None = None
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, *, only_failed: bool, legit_only: bool) -> None:
        if self.running:
            return
        self.thread = threading.Thread(
            target=self._run, args=(only_failed, legit_only), daemon=True
        )
        self.thread.start()

    def cancel(self) -> None:
        loop = self.loop
        ev = self.cancel_event
        if loop is None or ev is None:
            return
        loop.call_soon_threadsafe(ev.set)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.put((event, payload))

    def _run(self, only_failed: bool, legit_only: bool) -> None:
        try:
            cfg = self.app.cfg
            db = Database(cfg.general.db_path)
            try:
                docs = db.pending_or_failed(only_failed=only_failed)
                if not docs:
                    self._emit("info", {"msg": "Nothing to do."})
                    self._emit("run_done", {"total": 0, "succeeded": 0, "failed": 0,
                                            "per_source": {}})
                    return

                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.cancel_event = asyncio.Event()

                pipeline = Pipeline(
                    cfg, db,
                    legit_only=legit_only,
                    workers=cfg.general.workers,
                    progress_cb=self._emit,
                    cancel_event=self.cancel_event,
                )
                self.loop.run_until_complete(pipeline.run(docs))
            finally:
                db.close()
        except Exception as e:
            log.exception("Pipeline failed")
            self._emit("error", {"msg": str(e)})
            self._emit("run_done", {"total": 0, "succeeded": 0, "failed": 1,
                                    "per_source": {}})
        finally:
            if self.loop is not None:
                with contextlib.suppress(Exception):
                    self.loop.close()


# -----------------------------------------------------------------------------
# Worker thread that runs MultiSearcher
# -----------------------------------------------------------------------------


class SearchWorker:
    """Runs a MultiSearcher search in a background thread for the GUI."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.thread: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, sq: SearchQuery) -> None:
        if self.running:
            return
        self.thread = threading.Thread(target=self._run, args=(sq,), daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        loop = self._loop
        task = self._task
        if loop is None or task is None:
            return

        def _do_cancel() -> None:
            with contextlib.suppress(Exception):
                task.cancel()

        loop.call_soon_threadsafe(_do_cancel)

    def _emit(self, ev: str, payload: dict[str, Any]) -> None:
        self.events.put((ev, payload))

    def _run(self, sq: SearchQuery) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:

            async def go() -> SearchResult:
                async with Fetcher(
                    self.cfg.fetcher,
                    timeout_s=self.cfg.general.request_timeout_s,
                    max_retries=self.cfg.general.max_retries,
                ) as fetcher:
                    ms = MultiSearcher(fetcher)
                    return await ms.search(sq, progress_cb=self._emit)

            self._emit("search_start", {"sources": list(sq.sources), "query": sq.text})
            self._task = loop.create_task(go())
            try:
                result = loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                self._emit("search_error", {"msg": "Cancelled."})
                return
            self._emit(
                "search_done",
                {
                    "merged": list(result.merged),
                    "runs": [
                        {
                            "source": r.source,
                            "count": len(r.hits),
                            "elapsed_s": r.elapsed_s,
                            "error": r.error,
                        }
                        for r in result.runs
                    ],
                },
            )
        except Exception as e:
            log.exception("Search failed")
            self._emit("search_error", {"msg": str(e)})
        finally:
            with contextlib.suppress(Exception):
                loop.close()
            self._loop = None
            self._task = None


def _searcher_options_from_cfg(cfg: Config) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    cr = (cfg.metadata or {}).get("crossref") or {}
    if cr.get("mailto"):
        out["crossref"] = {"mailto": cr["mailto"]}
    oa = (cfg.metadata or {}).get("openalex") or {}
    if oa.get("mailto"):
        out["openalex"] = {"mailto": oa["mailto"]}
    ss = (cfg.metadata or {}).get("semantic_scholar") or {}
    if ss.get("api_key"):
        out["semantic_scholar"] = {"api_key": ss["api_key"]}
    annas = cfg.source("annas_archive").options or {}
    annas_opts: dict[str, Any] = {}
    if annas.get("mirrors"):
        annas_opts["mirrors"] = annas["mirrors"]
    if annas.get("base_url"):
        annas_opts["base_url"] = annas["base_url"]
    if annas_opts:
        out["annas_archive"] = annas_opts
    libgen = cfg.source("libgen").options or {}
    if libgen.get("mirrors"):
        out["libgen"] = {"mirrors": libgen["mirrors"]}
    zlib = cfg.source("zlibrary").options or {}
    if zlib.get("mirrors"):
        out["zlibrary"] = {"mirrors": zlib["mirrors"]}
    return out


# -----------------------------------------------------------------------------
# Settings dialog
# -----------------------------------------------------------------------------


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: App):
        super().__init__(parent.root)
        self.parent = parent
        self.cfg = parent.cfg
        self.title("Settings")
        self.transient(parent.root)
        self.grab_set()
        self.geometry("680x600")
        self.resizable(True, True)

        self._build()

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self._build_general_tab(nb)
        self._build_sources_tab(nb)
        self._build_metadata_tab(nb)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="Save", command=self._save, style="Accent.TButton").pack(
            side="right", padx=4
        )

    def _build_general_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="General")

        self.var_dl = tk.StringVar(value=str(self.cfg.general.download_dir))
        self.var_db = tk.StringVar(value=str(self.cfg.general.db_path))
        self.var_workers = tk.IntVar(value=self.cfg.general.workers)
        self.var_filename = tk.StringVar(value=self.cfg.general.filename_template)
        self.var_folder = tk.StringVar(value=self.cfg.general.folder_template)
        self.var_min_bytes = tk.IntVar(value=self.cfg.general.min_pdf_bytes)
        self.var_timeout = tk.IntVar(value=self.cfg.general.request_timeout_s)
        self.var_log = tk.StringVar(value=self.cfg.general.log_level)

        row = 0

        def add_path_row(label: str, var: tk.StringVar, choose: str) -> None:
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", padx=2, pady=4)
            ttk.Entry(f, textvariable=var, width=60).grid(row=row, column=1, sticky="ew",
                                                          padx=2, pady=4)
            ttk.Button(f, text="Browse",
                       command=lambda v=var, c=choose: self._browse(v, c)).grid(
                row=row, column=2, padx=2, pady=4)
            row += 1

        add_path_row("Download directory", self.var_dl, "dir")
        add_path_row("SQLite database", self.var_db, "save")

        def add_entry(label: str, var: tk.Variable) -> None:
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", padx=2, pady=4)
            ttk.Entry(f, textvariable=var, width=60).grid(row=row, column=1,
                                                          columnspan=2, sticky="ew",
                                                          padx=2, pady=4)
            row += 1

        add_entry("Workers (parallel docs)", self.var_workers)
        add_entry("Filename template", self.var_filename)
        add_entry("Folder template", self.var_folder)
        add_entry("Min PDF size (bytes)", self.var_min_bytes)
        add_entry("Request timeout (s)", self.var_timeout)

        ttk.Label(f, text="Log level").grid(row=row, column=0, sticky="w", padx=2, pady=4)
        ttk.Combobox(
            f, textvariable=self.var_log, state="readonly",
            values=["DEBUG", "INFO", "WARNING", "ERROR"], width=15,
        ).grid(row=row, column=1, sticky="w", padx=2, pady=4)
        row += 1

        f.columnconfigure(1, weight=1)

    def _build_sources_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Sources")

        ttk.Label(
            f,
            text=(
                "Tick the sources you want to use. Sources are tried top-to-bottom\n"
                "for every document. Shadow-library sources may host content under\n"
                "disputed legal status in your jurisdiction."
            ),
            foreground="#444",
        ).pack(anchor="w", pady=(0, 8))

        bulk = ttk.Frame(f)
        bulk.pack(fill="x", pady=(0, 6))
        ttk.Button(bulk, text="Enable shadow sources",
                   command=lambda: self._set_shadow(True)).pack(side="left", padx=2)
        ttk.Button(bulk, text="Disable shadow sources",
                   command=lambda: self._set_shadow(False)).pack(side="left", padx=2)

        self.source_vars: dict[str, tk.BooleanVar] = {}
        for name, label in ALL_SOURCES:
            sc = self.cfg.source(name)
            v = tk.BooleanVar(value=sc.enabled)
            self.source_vars[name] = v
            row = ttk.Frame(f)
            row.pack(fill="x", pady=1)
            ttk.Checkbutton(row, text=label, variable=v).pack(side="left", anchor="w")
            if name in SHADOW_SOURCES:
                ttk.Label(row, text="  shadow", foreground="#a04").pack(side="left")

    def _set_shadow(self, value: bool) -> None:
        for name in SHADOW_SOURCES:
            if name in self.source_vars and name != "zlibrary":
                self.source_vars[name].set(value)
        if not value and "zlibrary" in self.source_vars:
            self.source_vars["zlibrary"].set(False)

    def _build_metadata_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Metadata")

        unpaywall = (self.cfg.metadata.get("unpaywall") or {}).get("email", "")
        crossref = (self.cfg.metadata.get("crossref") or {}).get("mailto", "")
        openalex = (self.cfg.metadata.get("openalex") or {}).get("mailto", "")
        sschol = (self.cfg.metadata.get("semantic_scholar") or {}).get("api_key", "")

        self.var_unpaywall = tk.StringVar(value=unpaywall)
        self.var_crossref = tk.StringVar(value=crossref)
        self.var_openalex = tk.StringVar(value=openalex)
        self.var_sschol = tk.StringVar(value=sschol)

        rows = [
            ("Unpaywall email (required by their TOS)", self.var_unpaywall),
            ("Crossref mailto (polite pool)", self.var_crossref),
            ("OpenAlex mailto (polite pool)", self.var_openalex),
            ("Semantic Scholar API key (optional)", self.var_sschol),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", padx=2, pady=6)
            ttk.Entry(f, textvariable=var, width=50).grid(
                row=i, column=1, sticky="ew", padx=2, pady=6)

        ttk.Label(f, text=(
            "All fields are optional but recommended. They let APIs identify your\n"
            "client and route you to a higher-priority pool."
        ), foreground="#444").grid(row=len(rows), column=0, columnspan=2,
                                  sticky="w", pady=8)

        f.columnconfigure(1, weight=1)

    def _browse(self, var: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            chosen = filedialog.askdirectory(initialdir=var.get() or ".")
        else:
            chosen = filedialog.asksaveasfilename(
                initialdir=str(Path(var.get() or ".").parent) or ".",
                initialfile=Path(var.get() or "crawler.db").name,
            )
        if chosen:
            var.set(chosen)

    def _save(self) -> None:
        cfg = self.cfg
        # Path fields must be non-empty
        dl = self.var_dl.get().strip()
        db = self.var_db.get().strip()
        if not dl or not db:
            messagebox.showerror(
                "Settings", "Download directory and database path are required.",
                parent=self,
            )
            return

        new_general = replace(
            cfg.general,
            download_dir=Path(dl),
            db_path=Path(db),
            workers=_safe_int(self.var_workers, cfg.general.workers, lo=1, hi=64),
            filename_template=self.var_filename.get(),
            folder_template=self.var_folder.get(),
            min_pdf_bytes=_safe_int(self.var_min_bytes, cfg.general.min_pdf_bytes,
                                    lo=0),
            request_timeout_s=_safe_int(self.var_timeout, cfg.general.request_timeout_s,
                                        lo=1, hi=600),
            log_level=self.var_log.get() or "INFO",
        )
        cfg.general = new_general

        new_order: list[str] = []
        for name, _ in ALL_SOURCES:
            enabled = bool(self.source_vars[name].get())
            existing = cfg.sources.get(name) or SourceConfig(name=name)
            existing.enabled = enabled
            cfg.sources[name] = existing
            if enabled:
                new_order.append(name)
        cfg.sources_order = new_order

        cfg.metadata = dict(cfg.metadata or {})

        def _set_meta(section: str, key: str, value: str) -> None:
            v = value.strip()
            sec = dict(cfg.metadata.get(section) or {})
            if v:
                sec[key] = v
            else:
                sec.pop(key, None)
            if sec:
                cfg.metadata[section] = sec
            else:
                cfg.metadata.pop(section, None)

        _set_meta("unpaywall", "email", self.var_unpaywall.get())
        _set_meta("crossref", "mailto", self.var_crossref.get())
        _set_meta("openalex", "mailto", self.var_openalex.get())
        _set_meta("semantic_scholar", "api_key", self.var_sschol.get())

        try:
            write_config(cfg, self.parent.config_path)
        except OSError as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return

        self.parent.cfg = load_config(self.parent.config_path)
        self.parent.log(f"Saved settings to {self.parent.config_path}")
        self.destroy()


# -----------------------------------------------------------------------------
# Add document dialog
# -----------------------------------------------------------------------------


class AddDocDialog(tk.Toplevel):
    def __init__(self, parent: App):
        super().__init__(parent.root)
        self.parent = parent
        self.title("Add document")
        self.transient(parent.root)
        self.grab_set()
        self.geometry("520x340")

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        self.var_doi = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_authors = tk.StringVar()
        self.var_year = tk.StringVar()
        self.var_isbn = tk.StringVar()
        self.var_keywords = tk.StringVar()
        self.var_url = tk.StringVar()

        rows = [
            ("DOI", self.var_doi),
            ("Title", self.var_title),
            ("Authors  (semicolon-separated)", self.var_authors),
            ("Year", self.var_year),
            ("ISBN", self.var_isbn),
            ("Keywords  (comma-separated)", self.var_keywords),
            ("URL", self.var_url),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", padx=2, pady=4)
            ttk.Entry(f, textvariable=var, width=50).grid(row=i, column=1, sticky="ew",
                                                          padx=2, pady=4)
        f.columnconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="Add", command=self._save,
                   style="Accent.TButton").pack(side="right", padx=4)

    def _save(self) -> None:
        authors = [a.strip() for a in self.var_authors.get().split(";") if a.strip()]
        keywords = [k.strip() for k in self.var_keywords.get().split(",") if k.strip()]
        year_raw = self.var_year.get().strip()
        year: int | None
        if not year_raw:
            year = None
        elif year_raw.isdigit() and 1 <= int(year_raw) <= 9999:
            year = int(year_raw)
        else:
            messagebox.showerror(
                "Invalid year", f"'{year_raw}' is not a valid year.", parent=self,
            )
            return
        q = DocumentQuery(
            doi=normalize_doi(self.var_doi.get()),
            title=self.var_title.get().strip() or None,
            authors=authors,
            year=year,
            isbn=normalize_isbn(self.var_isbn.get()),
            keywords=keywords,
            url=self.var_url.get().strip() or None,
        )
        if q.is_empty():
            messagebox.showerror(
                "Missing fields",
                "Provide at least one of DOI, title, ISBN or URL.",
                parent=self,
            )
            return

        cfg = self.parent.cfg
        db = Database(cfg.general.db_path)
        try:
            doc_id = db.add_query(q)
        finally:
            db.close()
        self.parent.log(f"Queued document #{doc_id}")
        self.parent.refresh_table()
        self.destroy()


# -----------------------------------------------------------------------------
# Edit document dialog
# -----------------------------------------------------------------------------


class EditDocDialog(tk.Toplevel):
    def __init__(self, parent: App, doc: DocumentRow):
        super().__init__(parent.root)
        self.parent = parent
        self.doc = doc
        self.title(f"Edit document #{doc.id}")
        self.transient(parent.root)
        self.grab_set()
        self.geometry("540x340")

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        self.var_doi = tk.StringVar(value=doc.doi or "")
        self.var_title = tk.StringVar(value=doc.title or "")
        self.var_authors = tk.StringVar(value="; ".join(doc.authors or []))
        self.var_year = tk.StringVar(value=str(doc.year or ""))
        self.var_isbn = tk.StringVar(value=doc.isbn or "")
        self.var_url = tk.StringVar(value=doc.url or "")

        rows = [
            ("DOI", self.var_doi),
            ("Title", self.var_title),
            ("Authors", self.var_authors),
            ("Year", self.var_year),
            ("ISBN", self.var_isbn),
            ("URL", self.var_url),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", padx=2, pady=4)
            ttk.Entry(f, textvariable=var, width=60).grid(row=i, column=1, sticky="ew",
                                                          padx=2, pady=4)
        f.columnconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="Save", command=self._save,
                   style="Accent.TButton").pack(side="right", padx=4)

    def _save(self) -> None:
        year_raw = self.var_year.get().strip()
        year: int | None
        if not year_raw:
            year = None
        elif year_raw.isdigit() and 1 <= int(year_raw) <= 9999:
            year = int(year_raw)
        else:
            messagebox.showerror(
                "Invalid year",
                f"'{year_raw}' is not a valid year.",
                parent=self,
            )
            return
        authors = [a.strip() for a in self.var_authors.get().split(";") if a.strip()]
        cfg = self.parent.cfg
        db = Database(cfg.general.db_path)
        try:
            db.update_metadata(
                self.doc.id,
                doi=normalize_doi(self.var_doi.get()) or None,
                title=self.var_title.get().strip() or None,
                authors=authors or None,
                year=year,
                isbn=normalize_isbn(self.var_isbn.get()) or None,
                url=self.var_url.get().strip() or None,
            )
        except OSError as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return
        finally:
            db.close()
        self.parent.log(f"Updated #{self.doc.id}")
        self.parent.refresh_table()
        self.parent.show_detail(self.doc.id)
        self.destroy()


# -----------------------------------------------------------------------------
# Search dialog
# -----------------------------------------------------------------------------


class SearchDialog(tk.Toplevel):
    """Search across many sources, preview hits, queue selected."""

    def __init__(self, parent: App):
        super().__init__(parent.root)
        self.parent = parent
        self.title("Search sources")
        self.transient(parent.root)
        self.geometry("1080x680")

        # Hits keyed by tree iid -> SearchHit
        self.hits: dict[str, SearchHit] = {}
        self.normalized_hits: dict[str, NormalizedSearchHit] = {}
        self.worker = SearchWorker(parent.cfg)

        self._build()
        self._poll()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text="Query:").grid(row=0, column=0, sticky="w")
        self.var_query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.var_query, width=70)
        entry.grid(row=0, column=1, padx=4, sticky="ew")
        entry.bind("<Return>", lambda _e: self._do_search())
        entry.focus_set()

        self.var_kind = tk.StringVar(value="auto")
        ttk.Label(top, text="Kind:").grid(row=0, column=2, padx=(10, 2))
        ttk.Combobox(
            top, textvariable=self.var_kind, state="readonly", width=10,
            values=["auto", "doi", "isbn", "title", "author"],
        ).grid(row=0, column=3)

        self.btn_search = ttk.Button(top, text="Search", style="Accent.TButton",
                                     command=self._do_search)
        self.btn_search.grid(row=0, column=4, padx=8)

        self.btn_cancel = ttk.Button(top, text="Cancel", state="disabled",
                                     command=self._do_cancel)
        self.btn_cancel.grid(row=0, column=5)

        ttk.Label(top, text="Limit:").grid(row=0, column=6, padx=(10, 2))
        self.var_limit = tk.IntVar(value=15)
        ttk.Spinbox(top, from_=5, to=50, increment=5, textvariable=self.var_limit,
                    width=4).grid(row=0, column=7, padx=2)

        top.columnconfigure(1, weight=1)

        # Source toggles row
        srow = ttk.LabelFrame(self, text="Searchers", padding=(8, 4))
        srow.pack(fill="x", padx=10, pady=4)
        self.searcher_vars: dict[str, tk.BooleanVar] = {}
        col = 0
        for name, label, is_shadow in ALL_SEARCHERS:
            v = tk.BooleanVar(value=True)
            self.searcher_vars[name] = v
            text = label + ("  (shadow)" if is_shadow else "")
            cb = ttk.Checkbutton(srow, text=text, variable=v)
            cb.grid(row=0, column=col, sticky="w", padx=4, pady=2)
            col += 1
        ttk.Button(srow, text="OA only",
                   command=lambda: self._set_shadow_searchers(False)).grid(
            row=0, column=col, padx=8)
        ttk.Button(srow, text="All",
                   command=lambda: self._set_shadow_searchers(True)).grid(
            row=0, column=col + 1)

        # Per-source progress strip
        self.progress_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.progress_var, foreground="#445").pack(
            anchor="w", padx=12)

        # Results table
        body = ttk.Frame(self, padding=(10, 4))
        body.pack(fill="both", expand=True)

        cols = ("title", "authors", "year", "venue", "identifier", "availability", "sources")
        self.tree = ttk.Treeview(body, columns=cols, show="headings",
                                 selectmode="extended")
        widths = {"title": 340, "authors": 210, "year": 55, "venue": 190,
                  "identifier": 220, "availability": 90, "sources": 170}
        for c in cols:
            self.tree.heading(c, text=c.upper(),
                              command=lambda col=c: self._sort_by(col))
            self.tree.column(c, width=widths[c],
                             anchor="center" if c == "year" else "w",
                             stretch=(c in ("title", "authors", "venue", "identifier", "sources")))
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.tree.tag_configure("haspdf", foreground="#0a7a0a")
        self.tree.bind("<Double-1>", self._open_url)

        # Preview pane (abstract)
        preview_frame = ttk.LabelFrame(self, text="Preview", padding=8)
        preview_frame.pack(fill="x", padx=10, pady=(2, 4))
        self.preview = tk.Text(preview_frame, height=4, wrap="word", state="disabled",
                               background="#fbfbfb")
        self.preview.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Bottom action bar
        bar = ttk.Frame(self, padding=(10, 4, 10, 10))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="Queue all merged",
                   command=self._queue_all).pack(side="right", padx=4)
        ttk.Button(bar, text="Queue selected",
                   command=self._queue_selected,
                   style="Accent.TButton").pack(side="right", padx=4)
        ttk.Button(bar, text="Open page",
                   command=self._open_page).pack(side="right", padx=4)
        ttk.Button(bar, text="Open URL",
                   command=self._open_url).pack(side="right", padx=4)

    def _set_shadow_searchers(self, on: bool) -> None:
        for name in SHADOW_SEARCHERS:
            if name in self.searcher_vars:
                self.searcher_vars[name].set(on)
        if not on:
            return
        for name, _, _ in ALL_SEARCHERS:
            self.searcher_vars[name].set(True)

    def _do_search(self) -> None:
        query = self.var_query.get().strip()
        if not query:
            return
        if self.worker.running:
            return

        sources = [n for n, v in self.searcher_vars.items() if v.get()]
        if not sources:
            messagebox.showwarning("No searchers", "Select at least one source.",
                                   parent=self)
            return

        sq = SearchQuery(
            text=query,
            kind=self.var_kind.get() or "auto",
            sources=sources,
            limit_per_source=int(self.var_limit.get() or 15),
            options_per_source=_searcher_options_from_cfg(self.parent.cfg),
        )
        self._reset_results()
        self.btn_search.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status_var.set(f"Searching {len(sources)} source(s)...")
        self.progress_var.set("")
        self.worker.start(sq)

    def _do_cancel(self) -> None:
        if not self.worker.running:
            return
        self.worker.cancel()
        self.status_var.set("Cancelling search...")

    def _reset_results(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.hits.clear()
        self.normalized_hits.clear()
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.configure(state="disabled")

    def _on_select(self, _e: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        hit = self.hits.get(sel[0])
        normalized = self.normalized_hits.get(sel[0])
        if not hit or normalized is None:
            return
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("end", normalized.preview_text)
        self.preview.configure(state="disabled")

    def _open_url(self, _e: tk.Event | None = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        hit = self.hits.get(sel[0])
        if not hit:
            return
        target = hit.pdf_url or hit.url
        if target:
            webbrowser.open(target)

    def _open_page(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        hit = self.hits.get(sel[0])
        if not hit or not hit.url:
            return
        webbrowser.open(hit.url)

    def _queue_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        hits = [self.hits[i] for i in sel if i in self.hits]
        self._queue(hits)

    def _queue_all(self) -> None:
        if not self.tree.get_children():
            return
        hits = [self.hits[i] for i in self.tree.get_children() if i in self.hits]
        self._queue(hits)

    def _queue(self, hits: list[SearchHit]) -> None:
        if not hits:
            return
        cfg = self.parent.cfg
        db = Database(cfg.general.db_path)
        added = 0
        try:
            for h in hits:
                q = DocumentQuery(
                    doi=normalize_doi(h.doi) if h.doi else None,
                    title=h.title,
                    authors=list(h.authors or []),
                    year=h.year,
                    isbn=normalize_isbn(h.isbn) if h.isbn else None,
                    url=preferred_queue_url(h),
                )
                if q.is_empty():
                    continue
                try:
                    db.add_query(q)
                    added += 1
                except ValueError:
                    continue
        finally:
            db.close()
        self.parent.log(f"Queued {added} of {len(hits)} hits from search")
        self.parent.refresh_table()
        self.status_var.set(f"Queued {added} of {len(hits)} hits.")

    def _sort_by(self, col: str) -> None:
        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]

        def key(t: tuple[str, str]) -> tuple[int, Any]:
            v = t[0]
            try:
                return (0, float(v))
            except ValueError:
                return (1, v.lower())

        items.sort(key=key, reverse=(col == "year"))
        for index, (_, iid) in enumerate(items):
            self.tree.move(iid, "", index)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                ev, payload = self.worker.events.get_nowait()
                self._handle(ev, payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        with contextlib.suppress(tk.TclError):
            self.after(120, self._poll)

    def destroy(self) -> None:
        # Make sure a long-running search doesn't keep churning after the
        # dialog is closed.
        if self.worker.running:
            self.worker.cancel()
        super().destroy()

    def _handle(self, ev: str, payload: dict[str, Any]) -> None:
        if ev == "search_start":
            self.progress_var.set(
                "  •  ".join(f"{s}: …" for s in payload.get("sources", []))
            )
        elif ev == "searcher_done":
            src = payload.get("source")
            n = payload.get("count", 0)
            err = payload.get("error")
            text = self.progress_var.get()
            label = f"{src}: {n}" if not err else f"{src}: err({err[:30]})"
            self.progress_var.set(_replace_first(text, src, label))
        elif ev == "search_done":
            self.btn_search.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            merged: list[SearchHit] = list(payload.get("merged", []))
            self._populate(merged)
            runs = payload.get("runs") or []
            n = len(merged)
            self.status_var.set(
                f"{n} merged hits from {len(runs)} source(s)."
            )
        elif ev == "search_error":
            self.btn_search.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            self.status_var.set("Error: " + str(payload.get("msg")))

    def _populate(self, hits: list[SearchHit]) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.hits.clear()
        self.normalized_hits.clear()
        for i, h in enumerate(hits):
            normalized = normalize_search_hit(h)
            iid = f"hit{i}"
            tags = ("haspdf",) if h.has_pdf else ()
            self.tree.insert(
                "", "end", iid=iid,
                values=(
                    normalized.title[:180],
                    normalized.authors[:80],
                    normalized.year,
                    normalized.venue[:80],
                    normalized.identifier[:100],
                    normalized.availability,
                    normalized.sources[:80],
                ),
                tags=tags,
            )
            self.hits[iid] = h
            self.normalized_hits[iid] = normalized


def _replace_first(text: str, key: str, replacement: str) -> str:
    """In a `progress_var` string, replace the segment starting with `key:` with `replacement`."""
    parts = [p.strip() for p in text.split("•")]
    out: list[str] = []
    replaced = False
    for p in parts:
        if not replaced and p.startswith(key + ":"):
            out.append(replacement)
            replaced = True
        else:
            out.append(p)
    if not replaced:
        out.append(replacement)
    return "  •  ".join(out)


# -----------------------------------------------------------------------------
# Main app
# -----------------------------------------------------------------------------


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        if not self.config_path.exists():
            write_default_config(self.config_path)
        try:
            self.cfg = load_config(self.config_path)
        except Exception as e:
            log.exception("Could not load config %s", self.config_path)
            tk.Tk().withdraw()
            messagebox.showerror(
                "Config error",
                f"Could not load {self.config_path}:\n\n{e}\n\n"
                f"Delete or edit the file and try again.",
            )
            raise SystemExit(2) from e

        try:
            self.cfg.general.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Could not create download dir %s: %s",
                        self.cfg.general.download_dir, e)

        self.root = tk.Tk()
        self.root.title(f"documentcrawler {__version__}")
        # Clamp default size to the screen so we don't open offscreen on
        # small monitors.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = min(1380, sw - 40), min(820, sh - 80)
        self.root.geometry(f"{max(900, w)}x{max(600, h)}")
        self.root.minsize(900, 600)

        # all rows currently held in the queue tree, keyed by id
        self._row_cache: dict[int, DocumentRow] = {}
        self._current_doc_id: int | None = None
        # Becomes True once we've shown a closing-while-busy confirmation.
        self._closing = False

        self._setup_style()
        self._build_ui()

        self.worker = PipelineWorker(self)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()
        self._poll_events()
        self.refresh_table()

    # ---------- styling ----------

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            with contextlib.suppress(tk.TclError):
                style.theme_use(theme)
                break
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Detail.TLabel", foreground="#222")
        style.configure("DetailHeader.TLabel", font=("Segoe UI", 11, "bold"),
                        foreground="#111")
        style.configure("Subtle.TLabel", foreground="#666")

    # ---------- ui ----------

    def _build_ui(self) -> None:
        self._build_toolbar()
        self._build_filter_bar()

        # Horizontal split: left = queue + log,  right = detail panel
        h = ttk.PanedWindow(self.root, orient="horizontal")
        h.pack(fill="both", expand=True, padx=8, pady=4)

        # ---- left panel ----
        left = ttk.Frame(h)
        h.add(left, weight=3)

        # vertical: queue tree above, log below
        v = ttk.PanedWindow(left, orient="vertical")
        v.pack(fill="both", expand=True)

        table_frame = ttk.Frame(v)
        v.add(table_frame, weight=4)
        self._build_queue_tree(table_frame)

        bottom = ttk.Frame(v)
        v.add(bottom, weight=1)
        self._build_log_panel(bottom)

        # ---- right panel: detail ----
        right = ttk.Frame(h, padding=10)
        h.add(right, weight=2)
        self._build_detail_panel(right)

        # status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Frame(self.root)
        status_bar.pack(side="bottom", fill="x")
        ttk.Label(status_bar, textvariable=self.status_var, anchor="w",
                  padding=(8, 2)).pack(fill="x")

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(side="top", fill="x")

        ttk.Button(toolbar, text="Add",
                   command=self._open_add).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Search ▾",
                   command=self._open_search,
                   style="Accent.TButton").pack(side="left", padx=2)
        ttk.Button(toolbar, text="Import file",
                   command=self._import_file).pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_run = ttk.Button(toolbar, text="Run",
                                  command=self._run, style="Accent.TButton")
        self.btn_run.pack(side="left", padx=2)
        self.btn_cancel = ttk.Button(toolbar, text="Cancel",
                                     command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.var_only_failed = tk.BooleanVar(value=False)
        self.var_legit_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Only failed",
                        variable=self.var_only_failed).pack(side="left", padx=4)
        ttk.Checkbutton(toolbar, text="Legit only (no shadow)",
                        variable=self.var_legit_only).pack(side="left", padx=4)

        ttk.Button(toolbar, text="Retry failed",
                   command=self._retry_failed).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Refresh",
                   command=self.refresh_table).pack(side="left", padx=2)

        self.btn_settings = ttk.Button(toolbar, text="Settings",
                                       command=self._open_settings)
        self.btn_settings.pack(side="right", padx=2)
        ttk.Button(toolbar, text="?", width=3,
                   command=self._show_help).pack(side="right", padx=2)

    def _build_filter_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.var_filter = tk.StringVar()
        self.var_filter.trace_add("write", lambda *_: self._apply_filter())
        self._filter_entry = ttk.Entry(bar, textvariable=self.var_filter, width=40)
        self._filter_entry.pack(side="left", padx=4)

        ttk.Label(bar, text="Status:").pack(side="left", padx=(10, 2))
        self.var_status_filter = tk.StringVar(value="All")
        cb = ttk.Combobox(
            bar, textvariable=self.var_status_filter, state="readonly", width=12,
            values=[label for label, _ in STATUS_FILTERS],
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        ttk.Button(bar, text="Clear",
                   command=self._clear_filter).pack(side="left", padx=4)
        self.filter_count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.filter_count_var,
                  style="Subtle.TLabel").pack(side="left", padx=10)

    def _build_queue_tree(self, parent: ttk.Frame) -> None:
        self._normalized_row_cache: dict[int, NormalizedDocumentRow] = {}
        cols = ("title", "authors", "year", "identifier", "status", "file", "page")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings",
                                 selectmode="extended")
        widths = {"title": 320, "authors": 200, "year": 60, "identifier": 220,
                  "status": 100, "file": 280, "page": 260}
        for c in cols:
            self.tree.heading(c, text=c.upper(),
                              command=lambda col=c: self._sort_by(col))
            self.tree.column(
                c,
                width=widths[c],
                anchor="center" if c == "year" else "w",
                stretch=(c in ("title", "authors", "identifier", "file", "page")),
            )

        self.tree.tag_configure("done", foreground="#0a7a0a")
        self.tree.tag_configure("failed", foreground="#a40404")
        self.tree.tag_configure("pending", foreground="#555")
        self.tree.tag_configure("in_progress", foreground="#0a4a8a")

        ysb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._open_selected_file)
        self.tree.bind("<Delete>", self._delete_selected)

        self._build_context_menu()
        # right-click on Windows = Button-3
        self.tree.bind("<Button-3>", self._show_context_menu)

    def _build_context_menu(self) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Open file", command=self._action_open_file)
        m.add_command(label="Reveal in folder",
                      command=self._action_reveal_in_folder)
        m.add_separator()
        m.add_command(label="Open DOI in browser", command=self._action_open_doi)
        m.add_command(label="Open source page", command=self._action_open_page)
        m.add_command(label="Copy DOI", command=self._action_copy_doi)
        m.add_command(label="Copy URL", command=self._action_copy_url)
        m.add_command(label="Copy title", command=self._action_copy_title)
        m.add_separator()
        m.add_command(label="Edit metadata...", command=self._action_edit)
        m.add_command(label="Re-queue (set pending)", command=self._action_requeue)
        m.add_command(label="Re-search this title",
                      command=self._action_research_title)
        m.add_separator()
        m.add_command(label="Delete from queue", command=self._delete_selected)
        self._ctx_menu = m

    def _show_context_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        self.progress = ttk.Progressbar(parent, mode="determinate")
        self.progress.pack(fill="x", padx=2, pady=(2, 4))

        log_frame = ttk.Frame(parent)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=10, wrap="none", state="disabled",
                                background="#111", foreground="#dde",
                                font=("Consolas", 9))
        log_ysb = ttk.Scrollbar(log_frame, orient="vertical",
                                command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_ysb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_ysb.pack(side="right", fill="y")

    def _build_detail_panel(self, parent: ttk.Frame) -> None:
        self.detail_title = tk.StringVar(value="Select a document")
        self.detail_status = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.detail_title,
                  style="DetailHeader.TLabel", wraplength=420,
                  justify="left").pack(anchor="w")
        ttk.Label(parent, textvariable=self.detail_status,
                  style="Subtle.TLabel").pack(anchor="w", pady=(0, 8))

        # Metadata grid
        meta = ttk.Frame(parent)
        meta.pack(fill="x")
        self.detail_meta_vars: dict[str, tk.StringVar] = {}
        for i, key in enumerate(("DOI", "ISBN", "Year", "Authors", "URL", "File", "Error")):
            ttk.Label(meta, text=key + ":", style="Subtle.TLabel").grid(
                row=i, column=0, sticky="nw", padx=(0, 6), pady=2)
            v = tk.StringVar(value="")
            self.detail_meta_vars[key] = v
            ttk.Label(meta, textvariable=v, style="Detail.TLabel",
                      wraplength=380, justify="left").grid(
                row=i, column=1, sticky="w", pady=2)
        meta.columnconfigure(1, weight=1)

        # Action row
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=10)
        self.btn_open_file = ttk.Button(actions, text="Open file",
                                         command=self._action_open_file)
        self.btn_open_file.pack(side="left", padx=2)
        ttk.Button(actions, text="Reveal",
                   command=self._action_reveal_in_folder).pack(side="left", padx=2)
        ttk.Button(actions, text="Open DOI",
                   command=self._action_open_doi).pack(side="left", padx=2)
        ttk.Button(actions, text="Open page",
                   command=self._action_open_page).pack(side="left", padx=2)
        ttk.Button(actions, text="Copy DOI",
                   command=self._action_copy_doi).pack(side="left", padx=2)
        ttk.Button(actions, text="Copy URL",
                   command=self._action_copy_url).pack(side="left", padx=2)
        ttk.Button(actions, text="Edit",
                   command=self._action_edit).pack(side="left", padx=2)
        ttk.Button(actions, text="Re-queue",
                   command=self._action_requeue).pack(side="left", padx=2)

        # Attempts table
        ttk.Label(parent, text="Attempts",
                  style="DetailHeader.TLabel").pack(anchor="w", pady=(10, 4))
        a_frame = ttk.Frame(parent)
        a_frame.pack(fill="both", expand=True)
        cols = ("source", "ok", "status", "bytes", "when", "error")
        self.attempts_tree = ttk.Treeview(a_frame, columns=cols, show="headings",
                                          height=10)
        widths = {"source": 110, "ok": 50, "status": 60, "bytes": 80, "when": 130,
                  "error": 220}
        for c in cols:
            self.attempts_tree.heading(c, text=c.upper())
            self.attempts_tree.column(c, width=widths[c], anchor="w",
                                      stretch=(c == "error"))
        self.attempts_tree.tag_configure("ok", foreground="#0a7a0a")
        self.attempts_tree.tag_configure("err", foreground="#a40404")
        ysb = ttk.Scrollbar(a_frame, orient="vertical",
                            command=self.attempts_tree.yview)
        self.attempts_tree.configure(yscrollcommand=ysb.set)
        self.attempts_tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")

    # ---------- behaviour ----------

    def log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        # Cap the buffer so a multi-thousand-document run stays smooth.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > _LOG_MAX_LINES:
            drop = line_count - _LOG_MAX_LINES
            self.log_text.delete("1.0", f"{drop + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh_table(self) -> None:
        self._row_cache.clear()
        self._normalized_row_cache.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            rows = db.list_documents(limit=10000)
            counts = db.status_summary()
        finally:
            db.close()
        for r in rows:
            self._row_cache[r.id] = r
            self._normalized_row_cache[r.id] = normalize_document_row(r)
        self._render_rows()
        n = sum(counts.values())
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        pending = counts.get("pending", 0) + counts.get("in_progress", 0)
        if n == 0:
            self.status_var.set(
                "Queue empty — use Add (Ctrl+N), Import (Ctrl+O), "
                "or Search (Ctrl+K) to get started."
            )
        else:
            self.status_var.set(
                f"Queue: {n} total  |  done {done}  |  "
                f"failed {failed}  |  pending {pending}"
            )
        # Refresh detail panel for the currently selected doc
        if self._current_doc_id is not None:
            self.show_detail(self._current_doc_id)

    def _render_rows(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        filt = self.var_filter.get().strip().lower() if hasattr(self, "var_filter") else ""
        status_label = (
            self.var_status_filter.get() if hasattr(self, "var_status_filter") else "All"
        )
        status_value: DocStatus | None = None
        for label, value in STATUS_FILTERS:
            if label == status_label:
                status_value = value
                break

        shown = 0
        for r in self._row_cache.values():
            normalized = self._normalized_row_cache.get(r.id) or normalize_document_row(r)
            if status_value and r.status != status_value:
                continue
            if filt:
                hay = " ".join(
                    [
                        normalized.title,
                        normalized.authors,
                        normalized.year,
                        normalized.identifier,
                        normalized.status,
                        normalized.file,
                        normalized.page,
                    ]
                ).lower()
                if filt not in hay:
                    continue
            tag = r.status.value
            self.tree.insert(
                "", "end",
                iid=str(r.id),
                values=(
                    normalized.title[:140],
                    normalized.authors[:80],
                    normalized.year,
                    normalized.identifier[:100],
                    normalized.status,
                    normalized.file,
                    normalized.page,
                ),
                tags=(tag,),
            )
            shown += 1
        if hasattr(self, "filter_count_var"):
            total = len(self._row_cache)
            if shown == total:
                self.filter_count_var.set(f"{total} rows")
            else:
                self.filter_count_var.set(f"{shown} of {total} shown")

    def _apply_filter(self) -> None:
        self._render_rows()

    def _clear_filter(self) -> None:
        self.var_filter.set("")
        self.var_status_filter.set("All")
        self._render_rows()

    def _sort_by(self, col: str) -> None:
        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]
        try:
            items.sort(key=lambda x: int(x[0]) if x[0].isdigit() else x[0].lower())
        except Exception:
            items.sort(key=lambda x: x[0].lower())
        for index, (_, iid) in enumerate(items):
            self.tree.move(iid, "", index)

    # ---------- detail panel ----------

    def _on_tree_select(self, _e: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            doc_id = int(sel[0])
        except ValueError:
            return
        self.show_detail(doc_id)

    def show_detail(self, doc_id: int) -> None:
        self._current_doc_id = doc_id
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            doc = db.get(doc_id)
            attempts = db.attempts_for(doc_id) if doc else []
        finally:
            db.close()
        if not doc:
            self.detail_title.set("Document not found")
            self.detail_status.set("")
            for v in self.detail_meta_vars.values():
                v.set("")
            for iid in self.attempts_tree.get_children():
                self.attempts_tree.delete(iid)
            return

        self.detail_title.set(doc.title or "(no title)")
        self.detail_status.set(f"#{doc.id}  •  {doc.status.value}")
        self.detail_meta_vars["DOI"].set(doc.doi or "—")
        self.detail_meta_vars["ISBN"].set(doc.isbn or "—")
        self.detail_meta_vars["Year"].set(str(doc.year) if doc.year else "—")
        self.detail_meta_vars["Authors"].set("; ".join(doc.authors) or "—")
        self.detail_meta_vars["URL"].set(doc.url or "—")
        self.detail_meta_vars["File"].set(doc.file_path or "—")
        self.detail_meta_vars["Error"].set(doc.error or "—")

        for iid in self.attempts_tree.get_children():
            self.attempts_tree.delete(iid)
        for i, a in enumerate(attempts):
            tag = "ok" if a.success else "err"
            self.attempts_tree.insert(
                "", "end", iid=f"a{i}",
                values=(
                    a.source,
                    "yes" if a.success else "no",
                    str(a.http_status or ""),
                    str(a.bytes or ""),
                    a.started_at.strftime("%Y-%m-%d %H:%M"),
                    (a.error or a.candidate_url or "")[:200],
                ),
                tags=(tag,),
            )

    # ---------- actions (used from buttons + context menu) ----------

    def _selected_doc(self) -> DocumentRow | None:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self._row_cache.get(int(sel[0]))
        except ValueError:
            return None

    def _action_open_file(self) -> None:
        doc = self._selected_doc()
        if not doc or not doc.file_path:
            return
        path = Path(doc.file_path)
        if not path.exists():
            messagebox.showinfo("Missing file", str(path), parent=self.root)
            return
        _open_path(path)

    def _action_reveal_in_folder(self) -> None:
        doc = self._selected_doc()
        if not doc or not doc.file_path:
            return
        path = Path(doc.file_path)
        if not path.exists():
            messagebox.showinfo("Missing file", str(path), parent=self.root)
            return
        _reveal_in_folder(path)

    def _action_open_doi(self) -> None:
        doc = self._selected_doc()
        if not doc:
            return
        target: str | None = None
        if doc.doi:
            target = f"https://doi.org/{doc.doi}"
        elif doc.url:
            target = doc.url
        if target:
            webbrowser.open(target)

    def _action_open_page(self) -> None:
        doc = self._selected_doc()
        if not doc or not doc.url:
            return
        webbrowser.open(doc.url)

    def _action_copy_doi(self) -> None:
        doc = self._selected_doc()
        if not doc or not doc.doi:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(doc.doi)
        self.log(f"Copied DOI to clipboard: {doc.doi}")

    def _action_copy_url(self) -> None:
        doc = self._selected_doc()
        if not doc:
            return
        url = copyable_document_url(doc)
        if not url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.log(f"Copied URL to clipboard: {url}")

    def _action_copy_title(self) -> None:
        doc = self._selected_doc()
        if not doc or not doc.title:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(doc.title)
        self.log("Copied title to clipboard")

    def _action_edit(self) -> None:
        doc = self._selected_doc()
        if not doc:
            return
        EditDocDialog(self, doc)

    def _action_requeue(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            for iid in sel:
                with contextlib.suppress(ValueError):
                    db.set_status(int(iid), DocStatus.PENDING, error=None)
        finally:
            db.close()
        self.log(f"Re-queued {len(sel)} doc(s) -> pending")
        self.refresh_table()

    def _action_research_title(self) -> None:
        doc = self._selected_doc()
        if not doc:
            return
        dlg = SearchDialog(self)
        query = doc.title or doc.doi or ""
        dlg.var_query.set(query)
        dlg.var_kind.set("doi" if doc.doi and not doc.title else "auto")
        # Auto-fire the search after the dialog has rendered.
        if query:
            dlg.after(120, dlg._do_search)

    def _open_selected_file(self, _e: tk.Event) -> None:
        doc = self._selected_doc()
        if not doc:
            return
        if doc.file_path and Path(doc.file_path).exists():
            self._action_open_file()
        # Otherwise just keep the detail panel populated.

    # ---------- top-level handlers ----------

    def _open_add(self) -> None:
        AddDocDialog(self)

    def _open_settings(self) -> None:
        SettingsDialog(self)

    def _open_search(self) -> None:
        SearchDialog(self)

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Import references",
            filetypes=[
                ("All supported", "*.csv *.tsv *.bib *.bibtex *.ris *.txt *.lst"),
                ("CSV / TSV", "*.csv *.tsv"),
                ("BibTeX", "*.bib *.bibtex"),
                ("RIS", "*.ris"),
                ("Plain DOI list", "*.txt *.lst"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            queries = list(parse_file(Path(path)))
        except ValueError as e:
            messagebox.showerror("Import failed", str(e), parent=self.root)
            return
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            added, skipped = db.add_many(queries)
        finally:
            db.close()
        self.log(f"Imported {len(queries)} entries: added {added}, skipped {skipped}")
        self.refresh_table()

    def _run(self) -> None:
        if self.worker.running:
            return
        if not self.cfg.enabled_sources_in_order():
            messagebox.showwarning(
                "No sources enabled",
                "Open Settings -> Sources and enable at least one source.",
                parent=self.root,
            )
            return
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_settings.configure(state="disabled")
        self.progress.configure(value=0, maximum=100)
        self.log("=== Run started ===")
        self.worker.start(
            only_failed=self.var_only_failed.get(),
            legit_only=self.var_legit_only.get(),
        )

    def _cancel(self) -> None:
        self.worker.cancel()
        self.log("Cancellation requested...")

    def _retry_failed(self) -> None:
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            failed = db.list_documents(DocStatus.FAILED)
            for r in failed:
                db.set_status(r.id, DocStatus.PENDING, error=None)
        finally:
            db.close()
        self.log(f"Reset {len(failed)} failed -> pending")
        self.refresh_table()

    def _delete_selected(self, _event: tk.Event | None = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        if not messagebox.askyesno(
            "Delete?",
            f"Delete {len(sel)} document(s) from the queue? Files on disk are kept.",
            parent=self.root,
        ):
            return
        cfg = self.cfg
        db = Database(cfg.general.db_path)
        try:
            with db.transaction() as conn:
                for iid in sel:
                    with contextlib.suppress(ValueError):
                        conn.execute("DELETE FROM documents WHERE id = ?", (int(iid),))
        finally:
            db.close()
        self.refresh_table()

    # ---------- event pump ----------

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.worker.events.get_nowait()
                self._handle_event(event, payload)
        except queue.Empty:
            pass
        # Watchdog: if the worker thread died without emitting `run_done`
        # (e.g. an unhandled exception inside asyncio), restore the toolbar
        # so the user isn't locked out.
        if (
            not self.worker.running
            and str(self.btn_cancel.cget("state")) == "normal"
        ):
            self._reset_run_buttons()
            self.log("(worker exited unexpectedly — buttons reset)")
        self.root.after(150, self._poll_events)

    # ---------- shortcuts / close / help ----------

    def _bind_shortcuts(self) -> None:
        # Bind to the root window only (not bind_all) so modal dialogs that
        # call grab_set fully take over input.
        b = self.root.bind
        b("<Control-n>", lambda _e: self._open_add())
        b("<Control-N>", lambda _e: self._open_add())
        b("<Control-o>", lambda _e: self._import_file())
        b("<Control-O>", lambda _e: self._import_file())
        b("<Control-k>", lambda _e: self._open_search())
        b("<Control-K>", lambda _e: self._open_search())
        b("<Control-comma>", lambda _e: self._open_settings())
        b("<Control-r>", lambda _e: self._run())
        b("<Control-R>", lambda _e: self._run())
        b("<F5>",        lambda _e: self.refresh_table())
        b("<Control-period>", lambda _e: self._cancel())
        b("<Control-f>", lambda _e: self._focus_filter())
        b("<Control-F>", lambda _e: self._focus_filter())
        b("<Control-e>", lambda _e: self._action_edit())
        b("<Control-E>", lambda _e: self._action_edit())
        b("<F1>",        lambda _e: self._show_help())
        # Esc clears filter when filter entry has focus, otherwise cancels.
        b("<Escape>", self._on_escape)

    def _on_escape(self, _e: tk.Event) -> None:
        focused = self.root.focus_get()
        if focused is not None and isinstance(focused, ttk.Entry):
            self._clear_filter()
            return
        if self.worker.running:
            self._cancel()

    def _focus_filter(self) -> None:
        ent = getattr(self, "_filter_entry", None)
        if ent is None:
            return
        ent.focus_set()
        with contextlib.suppress(tk.TclError):
            ent.select_range(0, "end")

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.worker.running:
            ok = messagebox.askyesno(
                "Quit?",
                "A pipeline run is still in progress. "
                "Cancel it and quit anyway?",
                parent=self.root,
                default="no",
            )
            if not ok:
                return
            self._closing = True
            self.worker.cancel()
            # Give the thread a moment to clean up, then close.
            self.root.after(400, self._force_quit)
            return
        self._force_quit()

    def _force_quit(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            self.root.destroy()

    def _show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("documentcrawler — help")
        win.transient(self.root)
        win.geometry("560x520")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # --- About tab ---
        about = ttk.Frame(nb, padding=12)
        nb.add(about, text="About")
        ttk.Label(about, text=f"documentcrawler  {__version__}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(about, text="A search & download client for academic papers and books.\n"
                              "Open-access first, with optional shadow-library fallbacks.",
                  foreground="#444", justify="left").pack(anchor="w", pady=(4, 12))
        info = [
            ("Config",   str(self.config_path)),
            ("Database", str(self.cfg.general.db_path)),
            ("Downloads", str(self.cfg.general.download_dir)),
            ("Workers",  str(self.cfg.general.workers)),
        ]
        grid = ttk.Frame(about)
        grid.pack(fill="x")
        for i, (k, v) in enumerate(info):
            ttk.Label(grid, text=k + ":", style="Subtle.TLabel").grid(
                row=i, column=0, sticky="nw", padx=(0, 6), pady=2)
            ttk.Label(grid, text=v, wraplength=420, justify="left").grid(
                row=i, column=1, sticky="w", pady=2)

        # --- Shortcuts tab ---
        sf = ttk.Frame(nb, padding=12)
        nb.add(sf, text="Shortcuts")
        for i, (k, v) in enumerate(_SHORTCUTS):
            ttk.Label(sf, text=k, font=("Consolas", 10, "bold")).grid(
                row=i, column=0, sticky="w", padx=(0, 12), pady=2)
            ttk.Label(sf, text=v).grid(row=i, column=1, sticky="w", pady=2)

        # --- Tips tab ---
        tips = ttk.Frame(nb, padding=12)
        nb.add(tips, text="Tips")
        text = tk.Text(tips, wrap="word", height=18,
                       background="#fafafa", relief="flat")
        text.pack(fill="both", expand=True)
        text.insert("end",
            "•  Use Search (Ctrl+K) for free-text queries — it asks "
            "Crossref / OpenAlex / arXiv / Open Library / Semantic Scholar / "
            "Anna's Archive / LibGen in parallel and merges the hits.\n\n"
            "•  Use Add (Ctrl+N) when you already have a DOI or ISBN.\n\n"
            "•  Use Import (Ctrl+O) for .bib / .ris / .csv / DOI lists.\n\n"
            "•  Right-click any row in the queue to open the file, reveal "
            "it in the file manager, edit metadata, re-queue, or delete.\n\n"
            "•  The right-hand detail panel shows the per-source attempt "
            "history for the selected document.\n\n"
            "•  'Legit only' restricts a run to open-access / catalog "
            "sources (no shadow libraries).\n\n"
            "•  All settings live in config.toml. The CLI (documentcrawler) "
            "shares the same database.")
        text.configure(state="disabled")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

    def _handle_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "run_start":
            total = int(payload.get("total", 0))
            self.progress.configure(value=0, maximum=max(total, 1))
            self.log(f"Processing {total} documents")
        elif event == "doc_start":
            doc_id = payload.get("id")
            self.log(f"-> #{doc_id} {payload.get('doi') or payload.get('title') or ''}")
        elif event == "doc_done":
            ok = bool(payload.get("ok"))
            doc_id = payload.get("id")
            src = payload.get("source") or "-"
            completed = int(payload.get("completed", 0))
            total = int(payload.get("total", 1))
            self.progress.configure(value=completed, maximum=max(total, 1))
            tag = "ok" if ok else "fail"
            self.log(f"   #{doc_id}  {tag}  via {src}")
            self.refresh_table()
        elif event == "run_done":
            self._reset_run_buttons()
            self.log(
                f"=== Done. succeeded={payload.get('succeeded')} "
                f"failed={payload.get('failed')} ==="
            )
            per_source = payload.get("per_source") or {}
            for src, n in sorted(per_source.items(), key=lambda x: -x[1]):
                self.log(f"   via {src}: {n}")
            self.refresh_table()
        elif event == "info":
            self.log(payload.get("msg", ""))
        elif event == "error":
            self._reset_run_buttons()
            self.log(f"ERROR: {payload.get('msg', '')}")

    def _reset_run_buttons(self) -> None:
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.btn_settings.configure(state="normal")

    # ---------- entry ----------

    def run(self) -> None:
        self.root.mainloop()


# -----------------------------------------------------------------------------
# Helpers: open paths cross-platform
# -----------------------------------------------------------------------------


def _safe_int(
    var: tk.Variable,
    default: int,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    """Read an int from a Tk var; fall back to `default` on TclError or empty.

    Tk's IntVar.get() raises TclError if the entry is blank or non-numeric, so
    we always go via str-then-int with a try/except.
    """
    raw = ""
    try:
        raw = str(var.get()).strip()
    except tk.TclError:
        raw = ""
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _open_path(path: Path) -> None:
    """Open a file with the OS default application."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        log.warning("open failed for %s: %s", path, e)


def _reveal_in_folder(path: Path) -> None:
    """Reveal a file in the OS file manager."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception as e:
        log.warning("reveal failed for %s: %s", path, e)


def launch(config_path: Path | None = None) -> None:
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    App(cfg_path).run()
