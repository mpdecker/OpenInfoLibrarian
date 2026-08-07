"""Typer-based CLI."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

# Import sources / searchers to register them
import documentcrawler.searcher  # noqa: F401
import documentcrawler.sources  # noqa: F401
from documentcrawler import __version__
from documentcrawler.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    load_config,
    write_default_config,
)
from documentcrawler.db import Database
from documentcrawler.errors import CLIError, DocumentCrawlerError
from documentcrawler.exporters import (
    export_bibtex,
    export_csv,
    export_jsonl,
    export_ris,
)
from documentcrawler.fetcher import Fetcher
from documentcrawler.importers import parse_file
from documentcrawler.models import DocStatus, DocumentQuery
from documentcrawler.pipeline import Pipeline
from documentcrawler.searcher import MultiSearcher, SearchHit, SearchQuery
from documentcrawler.utils.logging import get_logger, setup_logging
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

app = typer.Typer(help="Find and download academic documents from many sources.",
                  no_args_is_help=True, add_completion=False)
db_app = typer.Typer(help="Database maintenance and optimization tools.")
app.add_typer(db_app, name="db", rich_help_panel="Maintenance")
webhooks_app = typer.Typer(help="Manage HTTP Webhook notification endpoints.")
app.add_typer(webhooks_app, name="webhooks", rich_help_panel="Services")
pdf_app = typer.Typer(help="PDF inspection and structure tools.")
app.add_typer(pdf_app, name="pdf", rich_help_panel="Maintenance")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    show_version: bool = typer.Option(False, "--version",
                                      help="Show the version and exit."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c",
                                     help="Path to config.toml."),
):
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    if show_version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(__version__)
        return


def _make_console() -> Console:
    """Build a Rich console that survives Windows code pages.

    On legacy ``cp1252`` terminals Rich crashes with ``UnicodeEncodeError``
    when it encounters non-Latin glyphs (CJK, accents from scraped pages,
    etc.).  We force UTF-8 and tell the underlying stream to substitute
    unrepresentable characters rather than raising.
    """

    stream = sys.stdout
    try:
        # Python 3.7+ — switch the stream to a forgiving encoder in-place.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return Console(force_terminal=stream.isatty(), soft_wrap=False)


console = _make_console()
log = get_logger("cli")


@contextmanager
def _loaded(config_path: Path | None = None) -> Generator[tuple[Config, Database], None, None]:
    cfg = load_config(config_path or DEFAULT_CONFIG_PATH)
    setup_logging(cfg.general.log_level)
    db = Database(cfg.general.db_path)
    try:
        yield cfg, db
    finally:
        db.close()


@app.command(rich_help_panel="Setup")
def init(
    ctx: typer.Context,
    with_examples: bool = typer.Option(
        False, "--with-examples",
        help="Also drop sample refs.bib / dois.txt / library.csv / references.ris "
             "files into ./examples so the README's quick-start commands work verbatim.",
    ),
    examples_dir: Path = typer.Option(
        Path("examples"), "--examples-dir",
        help="Where to write sample reference files when --with-examples is set.",
    ),
    force: bool = typer.Option(False, "--force",
        help="Overwrite existing config.toml instead of copying the example."),
) -> None:
    """Create a default config.toml and initialise the SQLite database."""
    config_path = ctx.obj["config_path"]
    if force and config_path.exists():
        config_path.unlink()
    target = write_default_config(config_path)
    with _loaded(config_path) as (cfg, db):
        cfg.general.download_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]wrote[/green] {target}")
        console.print(f"[green]initialised db[/green] {cfg.general.db_path}")
        console.print(f"[green]download dir[/green] {cfg.general.download_dir}")
    if with_examples:
        written = _write_examples(examples_dir)
        for path in written:
            console.print(f"[green]example[/green] {path}")
        if written:
            console.print(
                f"[cyan]Try:[/cyan] documentcrawler import "
                f"{written[0].as_posix()}"
            )


@app.command(rich_help_panel="Setup")
def examples(
    ctx: typer.Context,
    dest: Path = typer.Argument(
        Path("examples"),
        help="Directory to copy example reference files into. Defaults to ./examples.",
    ),
) -> None:
    """Copy bundled sample reference files (BibTeX / CSV / RIS / DOI list) to a directory."""
    written = _write_examples(dest)
    for path in written:
        console.print(f"[green]wrote[/green] {path}")
    if not written:
        console.print("[yellow]No bundled example files found.[/yellow]")
        raise typer.Exit(1)
    console.print(
        f"\n[cyan]Try:[/cyan]\n"
        f"  documentcrawler import {(dest / 'refs.bib').as_posix()}\n"
        f"  documentcrawler import {(dest / 'dois.txt').as_posix()}\n"
        f"  documentcrawler import {(dest / 'library.csv').as_posix()}"
    )


_EXAMPLE_FILES = ("refs.bib", "dois.txt", "library.csv", "references.ris")


def _write_examples(dest: Path) -> list[Path]:
    """Copy bundled example reference files into `dest`. Returns paths written."""
    from importlib.resources import files

    dest.mkdir(parents=True, exist_ok=True)
    package_dir = files("documentcrawler.examples")
    written: list[Path] = []
    for name in _EXAMPLE_FILES:
        src = package_dir.joinpath(name)
        if not src.is_file():
            continue
        target = dest / name
        target.write_bytes(src.read_bytes())
        written.append(target)
    return written


@app.command(name="add", rich_help_panel="Queue")
def add_cmd(
    ctx: typer.Context,
    doi: str | None = typer.Option(None, "--doi"),
    title: str | None = typer.Option(None, "--title", "-t"),
    author: list[str] = typer.Option(None, "--author", "-a", help="May be passed multiple times."),
    year: int | None = typer.Option(None, "--year", "-y"),
    isbn: str | None = typer.Option(None, "--isbn"),
    keyword: list[str] = typer.Option(None, "--keyword", "-k"),
    url: str | None = typer.Option(None, "--url"),
    priority: int = typer.Option(0, "--priority", "-p", help="Processing priority (higher runs first)."),
) -> None:
    """Enqueue a single document."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        query = DocumentQuery(
            doi=normalize_doi(doi),
            title=title,
            authors=list(author or []),
            year=year,
            isbn=normalize_isbn(isbn),
            keywords=list(keyword or []),
            url=url,
            priority=priority,
        )
        if query.is_empty():
            raise CLIError("Need at least one of --doi/--title/--isbn/--url")
        doc_id = db.add_query(query)
        console.print(f"[green]queued[/green] document #{doc_id}")


@app.command(name="add-batch", rich_help_panel="Queue")
def add_batch(
    ctx: typer.Context,
    references: list[str] = typer.Argument(
        None,
        help="List of DOIs, ISBNs, or URLs to enqueue.",
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f",
        help="Text file containing one DOI, ISBN, or URL per line.",
    ),
    priority: int = typer.Option(0, "--priority", "-p", help="Processing priority (higher runs first)."),
) -> None:
    """Enqueue multiple references at once from command line arguments or a text file."""
    targets: list[str] = list(references) if references else []
    if file:
        if not file.exists():
            raise CLIError(f"File not found: {file}")
        for line in file.read_text(encoding="utf-8").splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("#"):
                targets.append(line_str)

    if not targets:
        raise CLIError("Provide at least one reference argument or a valid --file.")

    queries: list[DocumentQuery] = []
    for ref in targets:
        doi = normalize_doi(ref)
        isbn = normalize_isbn(ref)
        if doi:
            queries.append(DocumentQuery(doi=doi, priority=priority))
        elif isbn:
            queries.append(DocumentQuery(isbn=isbn, priority=priority))
        elif ref.startswith(("http://", "https://")):
            queries.append(DocumentQuery(url=ref, priority=priority))
        else:
            queries.append(DocumentQuery(title=ref, priority=priority))

    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        added, skipped = db.add_many(queries)
        console.print(
            f"[green]enqueued[/green] {added} documents "
            f"([yellow]{skipped} skipped as duplicates[/yellow])"
        )


@app.command(name="import", rich_help_panel="Queue")
def import_cmd(
    ctx: typer.Context,
    file: Path = typer.Argument(..., dir_okay=False,
        help="Path to a .csv / .tsv / .bib / .ris / .txt file of references."),
) -> None:
    """Import documents from CSV / BibTeX / RIS / DOI list.

    Examples:

        documentcrawler import my_refs.bib
        documentcrawler import dois.txt
        documentcrawler import library.csv
    """
    if not file.exists():
        raise CLIError(f"File not found: {file}")
    if file.is_dir():
        raise CLIError(f"{file} is a directory, expected a file.")
    if not os.access(file, os.R_OK):
        raise CLIError(f"Cannot read {file} (permission denied).")

    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        try:
            queries = list(parse_file(file))
        except ValueError as exc:
            raise CLIError(f"Could not parse {file}: {exc}") from exc
        if not queries:
            console.print(
                f"[yellow]No references found in {file}.[/yellow] "
                f"Check that it's a CSV/BibTeX/RIS/DOI-list file."
            )
            return
        added, skipped = db.add_many(queries)
        console.print(f"[green]parsed[/green] {len(queries)} entries, "
                      f"[green]added[/green] {added}, [yellow]skipped[/yellow] {skipped}")


@app.command(rich_help_panel="Run")
def run(
    ctx: typer.Context,
    workers: int | None = typer.Option(None, "--workers", "-w"),
    sources: str | None = typer.Option(None, "--sources",
        help="Comma-separated source names that override config order."),
    only_failed: bool = typer.Option(False, "--only-failed",
        help="Only retry rows currently in 'failed' state."),
    retry_all: bool = typer.Option(False, "--retry-all",
        help="Also retry documents with permanent failures (invalid DOI, not a PDF, etc.)."),
    legit_only: bool = typer.Option(False, "--legit-only",
        help="Restrict to open-access sources (open_access, arxiv, pubmed, doaj)."),
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Search sources but skip download and write. Reports what would be downloaded."),
    timeout: int | None = typer.Option(None, "--timeout",
        help="Per-document pipeline timeout in seconds (overrides config)."),
) -> None:
    """Process pending (or failed) documents."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        docs = db.pending_or_failed(only_failed=only_failed, retry_permanent=retry_all)
        if not docs:
            console.print("[yellow]Nothing to do.[/yellow]")
            return

        sources_override = None
        if sources:
            sources_override = [s.strip() for s in sources.split(",") if s.strip()]

        pipeline = Pipeline(
            cfg, db,
            sources_override=sources_override,
            legit_only=legit_only,
            workers=workers if workers is not None else cfg.general.workers,
            dry_run=dry_run,
            per_doc_timeout_s=float(timeout) if timeout else cfg.general.pipeline_timeout_s,
        )
        summary = asyncio.run(pipeline.run(docs))

        table = Table(title="Run summary")
        table.add_column("metric")
        table.add_column("count", justify="right")
        table.add_row("total", str(summary.total))
        table.add_row("succeeded", f"[green]{summary.succeeded}[/green]")
        table.add_row("failed", f"[red]{summary.failed}[/red]")
        for src, n in sorted(summary.per_source.items(), key=lambda x: -x[1]):
            table.add_row(f"  via {src}", str(n))
        console.print(table)
        if summary.failed and summary.succeeded == 0:
            raise DocumentCrawlerError("All documents failed")


@app.command(rich_help_panel="Run")
def status(
    ctx: typer.Context,
) -> None:
    """Show queue counts and per-source success rates."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        counts = db.status_summary()
        sources = db.per_source_stats()

    t1 = Table(title="Documents by status")
    t1.add_column("status")
    t1.add_column("count", justify="right")
    for st in (DocStatus.PENDING, DocStatus.IN_PROGRESS, DocStatus.DONE, DocStatus.FAILED):
        t1.add_row(st.value, str(counts.get(st.value, 0)))
    console.print(t1)

    if sources:
        t2 = Table(title="Source attempts")
        t2.add_column("source")
        t2.add_column("success", justify="right")
        t2.add_column("fail", justify="right")
        t2.add_column("total", justify="right")
        t2.add_column("rate", justify="right")
        for s in sources:
            rate = (s["successes"] / s["total"] * 100.0) if s["total"] else 0.0
            t2.add_row(s["source"], str(s["successes"]), str(s["failures"]),
                       str(s["total"]), f"{rate:.0f}%")
        console.print(t2)


@app.command(name="list", rich_help_panel="Queue")
def list_cmd(
    ctx: typer.Context,
    status_filter: DocStatus | None = typer.Option(None, "--status", case_sensitive=False),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List documents in the queue."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        rows = db.list_documents(status=status_filter, limit=limit)
        total_count = sum(db.status_summary().values())
        if status_filter:
            total_count = sum(v for k, v in db.status_summary().items() if k == status_filter.value)
    table = Table(title=f"Documents ({len(rows)})")
    table.add_column("id", justify="right")
    table.add_column("status")
    table.add_column("doi")
    table.add_column("title")
    table.add_column("year")
    table.add_column("file")
    for r in rows:
        title = (r.title or "")[:60]
        table.add_row(
            str(r.id),
            _status_color(r.status),
            r.doi or "",
            title,
            str(r.year or ""),
            r.file_path or "",
        )
    console.print(table)
    if len(rows) == limit and total_count > limit:
        console.print(f"[dim]Showing {limit} of {total_count} documents. Use --limit to show more.[/dim]")


db_app = typer.Typer(help="Database maintenance and hot backup commands.")
app.add_typer(db_app, name="db", rich_help_panel="Maintenance")


@db_app.command(name="vacuum")
def db_vacuum(ctx: typer.Context) -> None:
    """Reclaim unused database space and optimize search indices."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        db.vacuum()
        console.print("[green]Database successfully vacuumed and indices optimized.[/green]")


@db_app.command(name="check")
def db_check(ctx: typer.Context) -> None:
    """Run SQLite integrity check."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        ok = db.integrity_check()
        if ok:
            console.print("[green]Database integrity check PASSED (ok).[/green]")
        else:
            console.print("[red]Database integrity check FAILED![/red]")
            raise typer.Exit(1)


@db_app.command(name="backup")
def db_backup(
    ctx: typer.Context,
    dest: Path = typer.Argument(..., help="Destination path for hot backup file (e.g. backup.db)."),
) -> None:
    """Perform a live thread-safe hot backup of the SQLite database."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        db.backup(dest)
        console.print(f"[green]Hot backup successfully written to {dest}[/green]")


@app.command(rich_help_panel="Maintenance")
def shell(ctx: typer.Context) -> None:
    """Launch an interactive REPL shell for DocumentCrawler."""
    console.print("[bold cyan]DocumentCrawler Interactive Shell[/bold cyan] (type 'help' or 'exit')")
    while True:
        try:
            cmd = input("documentcrawler> ").strip()
            if not cmd:
                continue
            if cmd in ("exit", "quit", "q"):
                console.print("[dim]Exiting shell.[/dim]")
                break
            if cmd == "help":
                console.print(
                    "[cyan]Available commands:[/cyan]\n"
                    "  status       - Show queue status summary\n"
                    "  diagnostics  - Show source health diagnostics\n"
                    "  vacuum       - Optimize database\n"
                    "  exit         - Exit REPL"
                )
                continue
            if cmd == "status":
                status(ctx)
            elif cmd == "vacuum":
                db_vacuum(ctx)
            else:
                console.print(f"[yellow]Unknown command: {cmd}. Type 'help' for options.[/yellow]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting shell.[/dim]")
            break


@app.command(rich_help_panel="Queue")
def show(
    ctx: typer.Context,
    doc_id: int = typer.Argument(...),
) -> None:
    """Show a single document plus its full attempt history."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        doc = db.get(doc_id)
        if not doc:
            raise CLIError(f"No such document #{doc_id}")
        attempts = db.attempts_for(doc_id)

    console.print(f"[bold]#{doc.id}[/bold]  {_status_color(doc.status)}")
    if doc.doi:
        console.print(f"  doi:    {doc.doi}")
    if doc.title:
        console.print(f"  title:  {doc.title}")
    if doc.authors:
        console.print(f"  authors: {', '.join(doc.authors)}")
    if doc.year:
        console.print(f"  year:   {doc.year}")
    if doc.file_path:
        console.print(f"  file:   {doc.file_path}")
    if doc.error:
        console.print(f"  error:  [red]{doc.error}[/red]")

    if attempts:
        t = Table(title="Attempts")
        t.add_column("source")
        t.add_column("ok")
        t.add_column("status", justify="right")
        t.add_column("bytes", justify="right")
        t.add_column("when")
        t.add_column("error / url")
        for a in attempts:
            t.add_row(
                a.source,
                "[green]yes[/green]" if a.success else "[red]no[/red]",
                str(a.http_status or ""),
                str(a.bytes or ""),
                a.started_at.strftime("%Y-%m-%d %H:%M"),
                (a.error or a.candidate_url or "")[:80],
            )
        console.print(t)


@app.command(rich_help_panel="Queue")
def retry(
    ctx: typer.Context,
    doc_id: int | None = typer.Argument(None),
    all_failed: bool = typer.Option(False, "--all-failed"),
    include_permanent: bool = typer.Option(False, "--include-permanent",
        help="Also reset documents with permanent failures."),
    source: str | None = typer.Option(None, "--source",
        help="Only reset documents whose last attempt was from the given source."),
) -> None:
    """Reset failed documents to pending so the next `run` retries them."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        if all_failed:
            if source:
                rows = db.failed_by_source(source)
            else:
                rows = db.pending_or_failed(only_failed=True, retry_permanent=include_permanent)
            for r in rows:
                db.set_status(r.id, DocStatus.PENDING, error=None)
            console.print(f"[green]reset[/green] {len(rows)} failed -> pending")
        elif doc_id is not None:
            doc = db.get(doc_id)
            if not doc:
                raise CLIError(f"No such document #{doc_id}")
            db.set_status(doc_id, DocStatus.PENDING, error=None)
            console.print(f"[green]reset[/green] #{doc_id} -> pending")
        else:
            raise CLIError("Pass a doc id or --all-failed")


_DEFAULT_SEARCH_SOURCES = [
    "crossref",
    "openalex",
    "arxiv",
    "core",
    "openlibrary",
    "semantic_scholar",
    "doi_org",
    "scihub",
    "annas_archive",
    "libgen",
    "zlibrary",
]
_LEGIT_SEARCH_SOURCES = {
    "crossref", "openalex", "arxiv", "core", "openlibrary", "semantic_scholar", "doi_org",
}


@app.command(rich_help_panel="Queue")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text search (or DOI / ISBN)."),
    sources: str | None = typer.Option(
        None, "--sources", "-s",
        help=f"Comma-separated searchers. Default: {','.join(_DEFAULT_SEARCH_SOURCES)}",
    ),
    kind: str = typer.Option(
        "auto", "--kind", "-k",
        help="auto | doi | isbn | title | author",
    ),
    limit: int = typer.Option(15, "--limit", "-n",
                              help="Max hits per source."),
    legit_only: bool = typer.Option(False, "--legit-only",
                                    help="Restrict to OA / catalog searchers."),
    queue_top: int = typer.Option(0, "--queue-top",
                                  help="After search, queue the top N merged results."),
    queue_all: bool = typer.Option(False, "--queue-all",
                                   help="Queue every merged hit."),
    timeout: float = typer.Option(
        30.0, "--timeout",
        help="Per-source timeout in seconds. Shadow library mirrors are often slow.",
    ),
    display_limit: int = typer.Option(50, "--display-limit",
        help="Max hits to display in the merged results table."),
) -> None:
    """Search multiple metadata + library sources for a query."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):

        src_list = (
            [s.strip() for s in sources.split(",") if s.strip()]
            if sources
            else list(_DEFAULT_SEARCH_SOURCES)
        )
        if legit_only:
            src_list = [s for s in src_list if s in _LEGIT_SEARCH_SOURCES]

        options = _searcher_options(cfg)
        sq = SearchQuery(
            text=query,
            kind=kind,
            sources=src_list,
            limit_per_source=limit,
            options_per_source=options,
            overall_timeout_s=max(5.0, timeout),
        )

        async def _run() -> Any:
            async with Fetcher(cfg.fetcher,
                               timeout_s=cfg.general.request_timeout_s,
                               max_retries=cfg.general.max_retries) as fetcher:
                ms = MultiSearcher(fetcher)
                return await ms.search(sq)

        result = asyncio.run(_run())

        runs_table = Table(title="Per-source")
        runs_table.add_column("source")
        runs_table.add_column("hits", justify="right")
        runs_table.add_column("time", justify="right")
        runs_table.add_column("error")
        for r in result.runs:
            runs_table.add_row(
                r.source, str(len(r.hits)), f"{r.elapsed_s:.2f}s", r.error or ""
            )
        console.print(runs_table)

        table = Table(title=f"Merged hits ({len(result.merged)})")
        table.add_column("#", justify="right")
        table.add_column("src")
        table.add_column("title")
        table.add_column("yr", justify="right")
        table.add_column("authors")
        table.add_column("doi/isbn")
        table.add_column("pdf")
        for i, h in enumerate(result.merged[:display_limit]):
            idref = h.doi or h.isbn or ""
            table.add_row(
                str(i + 1),
                h.source,
                (h.title or "")[:80],
                str(h.year or ""),
                h.author_str[:40],
                idref[:35],
                "[green]y[/green]" if h.has_pdf else "",
            )
        console.print(table)

        if queue_all or queue_top:
            to_queue: list[SearchHit] = (
                result.merged if queue_all else result.merged[: max(0, queue_top)]
            )
            added = 0
            for h in to_queue:
                q = _hit_to_query(h)
                if q.is_empty():
                    continue
                try:
                    db.add_query(q)
                    added += 1
                except ValueError:
                    continue
            console.print(f"[green]Queued {added} of {len(to_queue)} hits[/green]")


def _searcher_options(cfg: Config) -> dict[str, dict[str, Any]]:
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
    scihub = cfg.source("scihub").options or {}
    if scihub.get("mirrors"):
        out["scihub"] = {"mirrors": scihub["mirrors"]}
    return out


def _hit_to_query(h: SearchHit) -> DocumentQuery:
    return DocumentQuery(
        doi=normalize_doi(h.doi) if h.doi else None,
        title=h.title,
        authors=list(h.authors or []),
        year=h.year,
        isbn=normalize_isbn(h.isbn) if h.isbn else None,
        url=h.pdf_url or h.url,
        extra={"search_source": h.source, "search_score": h.score, **(h.extra or {})},
    )


@app.command(rich_help_panel="Services")
def serve(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", "-h"),
    port: int = typer.Option(8099, "--port", "-p"),
) -> None:
    """Start an HTTP acquisition server (for Zetetic / SecondBrain integration)."""
    try:
        from documentcrawler.server import start_server
    except ImportError as e:
        raise CLIError(f"Server unavailable: {e}") from e
    start_server(host, port, ctx.obj["config_path"])


@app.command(rich_help_panel="Services")
def gui(
    ctx: typer.Context,
) -> None:
    """Launch the desktop GUI."""
    try:
        from documentcrawler.gui import launch
    except ImportError as e:
        raise CLIError(f"GUI unavailable: {e}") from e
    launch(ctx.obj["config_path"])


@app.command(rich_help_panel="Setup")
def sources(
    ctx: typer.Context,
) -> None:
    """List configured sources and whether each is enabled."""
    with _loaded(ctx.obj["config_path"]) as (cfg, _db):
        t = Table(title="Sources")
        t.add_column("name")
        t.add_column("enabled")
        t.add_column("order")
        order_map = {n: i for i, n in enumerate(cfg.sources_order)}
        all_names = sorted(set(cfg.sources_order) | set(cfg.sources.keys()))
        for n in all_names:
            sc = cfg.source(n)
            t.add_row(
                n,
                "[green]yes[/green]" if sc.enabled else "[dim]no[/dim]",
                str(order_map.get(n, "-")),
            )
        console.print(t)


@app.command(rich_help_panel="Queue management")
def export(
    ctx: typer.Context,
    format: str = typer.Option(
        "bibtex", "--format", "-f",
        help="Export format: bibtex, ris, csv, json.",
    ),
    status: str = typer.Option(
        "done", "--status", "-s",
        help="Filter documents by status: done, pending, failed, or all.",
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o",
        help="File path to write output. If omitted, prints to stdout.",
    ),
):
    """Export queued or completed documents to BibTeX, RIS, CSV, or JSON Lines."""
    config_path = ctx.obj.get("config_path")
    with _loaded(config_path) as (_, db):
        status_filter: DocStatus | None = None
        if status.lower() != "all":
            try:
                status_filter = DocStatus(status.lower())
            except ValueError as err:
                raise CLIError(f"Invalid status filter: {status}. Use done, pending, failed, or all.") from err

        docs = db.list_documents(status=status_filter, limit=10000)
        fmt = format.lower()
        if fmt in ("bib", "bibtex"):
            content = export_bibtex(docs)
        elif fmt == "ris":
            content = export_ris(docs)
        elif fmt == "csv":
            content = export_csv(docs)
        elif fmt in ("json", "jsonl"):
            content = export_jsonl(docs)
        else:
            raise CLIError(f"Unsupported export format: {format}. Use bibtex, ris, csv, or json.")

        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
            console.print(f"[green]Exported {len(docs)} documents to {output}[/green]")
        else:
            console.print(content)


@app.command(rich_help_panel="Maintenance")
def diagnostics(ctx: typer.Context) -> None:
    """Display diagnostic health & telemetry metrics across all search and download sources."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        stats = db.get_source_diagnostics()
        if not stats:
            console.print("[yellow]No source attempt telemetry available in database yet.[/yellow]")
            return

        table = Table(title="Source Health & Telemetry Diagnostics", show_lines=True)
        table.add_column("Source", style="cyan", no_wrap=True)
        table.add_column("Attempts", justify="right")
        table.add_column("Successes", justify="right", style="green")
        table.add_column("Success Rate", justify="right", style="bold green")
        table.add_column("Bandwidth (MB)", justify="right")
        table.add_column("Transient Errs", justify="right", style="yellow")
        table.add_column("Permanent Errs", justify="right", style="red")

        for s in stats:
            mb = round(s["total_bytes"] / (1024 * 1024), 2)
            rate_fmt = f"{s['success_rate']}%"
            table.add_row(
                s["source"],
                str(s["total_attempts"]),
                str(s["successful_attempts"]),
                rate_fmt,
                f"{mb} MB",
                str(s["transient_errors"]),
                str(s["permanent_errors"]),
            )

        console.print(table)


@app.command(name="search-fts", rich_help_panel="Queue")
def search_fts_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Full-text search query string."),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results to return."),
) -> None:
    """Execute instant SQLite FTS5 full-text search across titles, authors, DOIs, and ISBNs."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        rows = db.search_fts(query, limit=limit)
        if not rows:
            console.print(f"[yellow]No documents found matching FTS query: '{query}'[/yellow]")
            return

        table = Table(title=f"FTS Search Results ({len(rows)})")
        table.add_column("id", justify="right")
        table.add_column("status")
        table.add_column("doi")
        table.add_column("title")
        table.add_column("authors")
        table.add_column("year")

        for r in rows:
            title = (r.title or "")[:60]
            table.add_row(
                str(r.id),
                _status_color(r.status),
                r.doi or "",
                title,
                ", ".join(r.authors[:2]),
                str(r.year or ""),
            )
        console.print(table)


@db_app.command(name="optimize-fts")
def db_optimize_fts_command(ctx: typer.Context) -> None:
    """Optimize SQLite FTS5 index structure to merge B-tree segments."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        db.optimize_fts()
        console.print("[green]Successfully optimized FTS5 full-text index.[/green]")


@db_app.command(name="status")
def db_status_command(ctx: typer.Context) -> None:
    """Display database integrity status, size metrics, and table counts."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        stats = db.get_db_stats()
        console.print(f"[bold cyan]Database Status:[/bold cyan] {stats['path']}")
        console.print(f"  Size:       {stats['size_mb']} MB ({stats['size_bytes']} bytes)")
        console.print("  Integrity:  [green]OK[/green]" if stats["integrity_ok"] else "  Integrity:  [red]FAILED[/red]")
        console.print(f"  Total Docs: {stats['total_documents']}")
        console.print(f"  Attempts:   {stats['total_attempts']}")
        console.print(f"  Webhooks:   {stats['total_webhooks']}")


@db_app.command(name="dedupe")
def db_dedupe_command(
    ctx: typer.Context,
    threshold: float = typer.Option(0.85, "--threshold", "-t", help="Title similarity threshold (0.5 - 1.0)."),
    strategy: str = typer.Option("smart", "--strategy", "-s", help="Strategy: exact, fuzzy, or smart."),
    merge: bool = typer.Option(False, "--merge", "-m", help="Automatically merge duplicate clusters."),
) -> None:
    """Find and merge duplicate document records."""
    from documentcrawler.dedupe import find_duplicate_clusters

    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        clusters = find_duplicate_clusters(db, threshold=threshold, strategy=strategy)  # type: ignore[arg-type]
        if not clusters:
            console.print("[green]No duplicate document clusters found.[/green]")
            return

        console.print(f"[bold cyan]Found {len(clusters)} duplicate cluster(s) via [{strategy}]:[/bold cyan]")
        for idx, cluster in enumerate(clusters, 1):
            primary = cluster["primary"]
            secondaries = cluster["secondaries"]
            reason = cluster["match_reason"]
            confidence = cluster["confidence"]

            title_sample = (primary.title or "Untitled")[:40]
            console.print(
                f"  Cluster #{idx} [{reason} conf={confidence:.2f}]: "
                f"Primary #{primary.id} '{title_sample}'"
            )
            for doc in secondaries:
                dup_title = (doc.title or "Untitled")[:40]
                console.print(f"    - Secondary #{doc.id} '{dup_title}'")

        if merge:
            total_merged = 0
            for cluster in clusters:
                primary = cluster["primary"]
                secondary_ids = [d.id for d in cluster["secondaries"]]
                db.merge_documents(primary.id, secondary_ids)
                total_merged += len(secondary_ids)
            console.print(f"[green]Successfully merged {total_merged} duplicate records.[/green]")
        else:
            console.print("[dim]Use --merge to consolidate duplicate records into primary documents.[/dim]")


@webhooks_app.command(name="add")
def webhook_add(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Webhook HTTP target URL."),
    events: str = typer.Option("*", "--events", "-e", help="Comma-separated event names or '*'."),
    secret: str | None = typer.Option(None, "--secret", "-s", help="Optional HMAC signature secret."),
) -> None:
    """Register a new HTTP Webhook target."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        webhook_id = db.add_webhook(url, events=events, secret=secret)
        console.print(f"[green]Registered webhook #{webhook_id}[/green] ({url})")


@webhooks_app.command(name="list")
def webhook_list(ctx: typer.Context) -> None:
    """List all registered HTTP Webhooks."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        hooks = db.list_webhooks()
        if not hooks:
            console.print("[yellow]No webhooks registered.[/yellow]")
            return
        table = Table(title=f"Registered Webhooks ({len(hooks)})")
        table.add_column("id", justify="right")
        table.add_column("url")
        table.add_column("events")
        table.add_column("secret")
        table.add_column("created_at")
        for h in hooks:
            table.add_row(
                str(h["id"]),
                h["url"],
                h["events"],
                "[cyan]configured[/cyan]" if h["secret"] else "[dim]none[/dim]",
                h["created_at"],
            )
        console.print(table)


@webhooks_app.command(name="delete")
def webhook_delete(
    ctx: typer.Context,
    webhook_id: int = typer.Argument(..., help="ID of webhook to delete."),
) -> None:
    """Delete a registered HTTP Webhook."""
    with _loaded(ctx.obj["config_path"]) as (cfg, db):
        ok = db.delete_webhook(webhook_id)
        if ok:
            console.print(f"[green]Deleted webhook #{webhook_id}.[/green]")
        else:
            console.print(f"[red]Webhook #{webhook_id} not found.[/red]")
            raise typer.Exit(1)


@pdf_app.command(name="inspect")
def pdf_inspect_command(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Path to PDF file."),
) -> None:
    """Inspect PDF document properties, page count, catalog metadata, and text sample."""
    from documentcrawler.pdf import inspect_pdf

    try:
        info = inspect_pdf(path)
    except Exception as e:
        raise CLIError(f"Failed to inspect PDF {path}: {e}") from e

    console.print(f"[bold cyan]PDF Inspection:[/bold cyan] {info['file_path']}")
    console.print(f"  Size:     {info['file_size_kb']} KB ({info['file_size_bytes']} bytes)")
    console.print(f"  Version:  PDF-{info['pdf_version']}")
    console.print(f"  Pages:    {info['page_count']}")
    if info.get("metadata"):
        console.print("  Metadata:")
        for k, v in info["metadata"].items():
            console.print(f"    {k}: {v}")
    if info.get("text_sample"):
        console.print(f"  Text Sample: {info['text_sample'][:200]}...")


def _status_color(s: DocStatus) -> str:
    color = {
        DocStatus.PENDING: "yellow",
        DocStatus.IN_PROGRESS: "cyan",
        DocStatus.DONE: "green",
        DocStatus.FAILED: "red",
    }.get(s, "white")
    return f"[{color}]{s.value}[/{color}]"


def _main():
    # Preprocess sys.argv to move global options (--config or -c) to the front
    args = sys.argv[1:]
    config_indices = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--config" or arg == "-c":
            config_indices.append((i, True))
            i += 2
        elif arg.startswith("--config="):
            config_indices.append((i, False))
            i += 1
        else:
            i += 1
    
    if config_indices:
        extracted = []
        for idx, needs_val in reversed(config_indices):
            if needs_val:
                if idx + 1 < len(args):
                    val = args.pop(idx + 1)
                    name = args.pop(idx)
                    extracted.insert(0, val)
                    extracted.insert(0, name)
            else:
                val = args.pop(idx)
                extracted.insert(0, val)
        sys.argv = [sys.argv[0]] + extracted + args

    try:
        app()
    except CLIError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    except DocumentCrawlerError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    _main()
