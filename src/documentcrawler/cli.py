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
from documentcrawler.fetcher import Fetcher
from documentcrawler.importers import parse_file
from documentcrawler.models import DocStatus, DocumentQuery
from documentcrawler.pipeline import Pipeline
from documentcrawler.searcher import MultiSearcher, SearchHit, SearchQuery
from documentcrawler.utils.logging import get_logger, setup_logging
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

app = typer.Typer(help="Find and download academic documents from many sources.",
                  no_args_is_help=True, add_completion=False)


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
    import sys

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
        )
        if query.is_empty():
            raise CLIError("Need at least one of --doi/--title/--isbn/--url")
        doc_id = db.add_query(query)
        console.print(f"[green]queued[/green] document #{doc_id}")


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


def _status_color(s: DocStatus) -> str:
    color = {
        DocStatus.PENDING: "yellow",
        DocStatus.IN_PROGRESS: "cyan",
        DocStatus.DONE: "green",
        DocStatus.FAILED: "red",
    }.get(s, "white")
    return f"[{color}]{s.value}[/{color}]"


def _main():
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
