"""Typer-based CLI."""

from __future__ import annotations

import asyncio
import os
import sys
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
from documentcrawler.fetcher import Fetcher
from documentcrawler.importers import parse_file
from documentcrawler.models import DocStatus, DocumentQuery
from documentcrawler.pipeline import Pipeline
from documentcrawler.searcher import MultiSearcher, SearchHit, SearchQuery
from documentcrawler.utils.logging import get_logger, setup_logging
from documentcrawler.utils.sanitize import normalize_doi, normalize_isbn

app = typer.Typer(help="Find and download academic documents from many sources.",
                  no_args_is_help=True, add_completion=False)


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


def _load(config_path: Path | None = None) -> tuple[Config, Database]:
    cfg = load_config(config_path or DEFAULT_CONFIG_PATH)
    setup_logging(cfg.general.log_level)
    db = Database(cfg.general.db_path)
    return cfg, db


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@app.command()
def init(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    with_examples: bool = typer.Option(
        False, "--with-examples",
        help="Also drop sample refs.bib / dois.txt / library.csv / references.ris "
             "files into ./examples so the README's quick-start commands work verbatim.",
    ),
    examples_dir: Path = typer.Option(
        Path("examples"), "--examples-dir",
        help="Where to write sample reference files when --with-examples is set.",
    ),
) -> None:
    """Create a default config.toml and initialise the SQLite database."""
    target = write_default_config(config_path)
    cfg = load_config(config_path)
    db = Database(cfg.general.db_path)
    db.close()
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


@app.command()
def examples(
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


@app.command(name="add")
def add_cmd(
    doi: str | None = typer.Option(None, "--doi"),
    title: str | None = typer.Option(None, "--title", "-t"),
    author: list[str] = typer.Option(None, "--author", "-a", help="May be passed multiple times."),
    year: int | None = typer.Option(None, "--year", "-y"),
    isbn: str | None = typer.Option(None, "--isbn"),
    keyword: list[str] = typer.Option(None, "--keyword", "-k"),
    url: str | None = typer.Option(None, "--url"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Enqueue a single document."""
    cfg, db = _load(config_path)
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
        console.print("[red]Need at least one of --doi/--title/--isbn/--url[/red]")
        raise typer.Exit(2)
    doc_id = db.add_query(query)
    db.close()
    console.print(f"[green]queued[/green] document #{doc_id}")


@app.command(name="import")
def import_cmd(
    file: Path = typer.Argument(..., dir_okay=False,
        help="Path to a .csv / .tsv / .bib / .ris / .txt file of references."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Import documents from CSV / BibTeX / RIS / DOI list.

    Examples:

        documentcrawler import my_refs.bib
        documentcrawler import dois.txt
        documentcrawler import library.csv
    """
    if not file.exists():
        console.print(
            f"[red]File not found:[/red] {file}\n"
            f"[yellow]Tip:[/yellow] the README uses [bold]refs.bib[/bold] as a "
            f"placeholder — point this at one of your own files.\n"
            f"Supported formats: .csv .tsv .bib .ris .txt (one DOI per line)."
        )
        raise typer.Exit(2)
    if file.is_dir():
        console.print(f"[red]{file} is a directory, expected a file.[/red]")
        raise typer.Exit(2)
    if not os.access(file, os.R_OK):
        console.print(f"[red]Cannot read {file} (permission denied).[/red]")
        raise typer.Exit(2)

    cfg, db = _load(config_path)
    try:
        queries = list(parse_file(file))
    except ValueError as exc:
        console.print(f"[red]Could not parse {file}:[/red] {exc}")
        db.close()
        raise typer.Exit(1) from exc
    if not queries:
        console.print(
            f"[yellow]No references found in {file}.[/yellow] "
            f"Check that it's a CSV/BibTeX/RIS/DOI-list file."
        )
        db.close()
        return
    added, skipped = db.add_many(queries)
    db.close()
    console.print(f"[green]parsed[/green] {len(queries)} entries, "
                  f"[green]added[/green] {added}, [yellow]skipped[/yellow] {skipped}")


@app.command()
def run(
    workers: int | None = typer.Option(None, "--workers", "-w"),
    sources: str | None = typer.Option(None, "--sources",
        help="Comma-separated source names that override config order."),
    only_failed: bool = typer.Option(False, "--only-failed",
        help="Only retry rows currently in 'failed' state."),
    legit_only: bool = typer.Option(False, "--legit-only",
        help="Restrict to open-access sources (open_access, arxiv, pubmed, doaj)."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Process pending (or failed) documents."""
    cfg, db = _load(config_path)
    docs = db.pending_or_failed(only_failed=only_failed)
    if not docs:
        console.print("[yellow]Nothing to do.[/yellow]")
        db.close()
        return

    sources_override = None
    if sources:
        sources_override = [s.strip() for s in sources.split(",") if s.strip()]

    pipeline = Pipeline(
        cfg, db,
        sources_override=sources_override,
        legit_only=legit_only,
        workers=workers or cfg.general.workers,
    )
    summary = asyncio.run(pipeline.run(docs))
    db.close()

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
        sys.exit(1)


@app.command()
def status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Show queue counts and per-source success rates."""
    cfg, db = _load(config_path)
    counts = db.status_summary()
    sources = db.per_source_stats()
    db.close()

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


@app.command(name="list")
def list_cmd(
    status_filter: str | None = typer.Option(None, "--status",
        help="pending|in_progress|done|failed"),
    limit: int = typer.Option(50, "--limit", "-n"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """List documents in the queue."""
    cfg, db = _load(config_path)
    st = DocStatus(status_filter) if status_filter else None
    rows = db.list_documents(status=st, limit=limit)
    db.close()
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


@app.command()
def show(
    doc_id: int = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Show a single document plus its full attempt history."""
    cfg, db = _load(config_path)
    doc = db.get(doc_id)
    if not doc:
        console.print(f"[red]No such document #{doc_id}[/red]")
        db.close()
        raise typer.Exit(2)
    attempts = db.attempts_for(doc_id)
    db.close()

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


@app.command()
def retry(
    doc_id: int | None = typer.Argument(None),
    all_failed: bool = typer.Option(False, "--all-failed"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Reset failed documents to pending so the next `run` retries them."""
    cfg, db = _load(config_path)
    if all_failed:
        rows = db.list_documents(DocStatus.FAILED)
        for r in rows:
            db.set_status(r.id, DocStatus.PENDING, error=None)
        console.print(f"[green]reset[/green] {len(rows)} failed -> pending")
    elif doc_id is not None:
        doc = db.get(doc_id)
        if not doc:
            console.print(f"[red]No such document #{doc_id}[/red]")
            db.close()
            raise typer.Exit(2)
        db.set_status(doc_id, DocStatus.PENDING, error=None)
        console.print(f"[green]reset[/green] #{doc_id} -> pending")
    else:
        console.print("[red]Pass a doc id or --all-failed[/red]")
        db.close()
        raise typer.Exit(2)
    db.close()


_DEFAULT_SEARCH_SOURCES = [
    "crossref",
    "openalex",
    "arxiv",
    "openlibrary",
    "semantic_scholar",
    "annas_archive",
    "libgen",
    "zlibrary",
]
_LEGIT_SEARCH_SOURCES = {"crossref", "openalex", "arxiv", "openlibrary", "semantic_scholar"}


@app.command()
def search(
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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Search multiple metadata + library sources for a query."""
    cfg, db = _load(config_path)

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
    for i, h in enumerate(result.merged[:50]):
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
    db.close()


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


@app.command()
def gui(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Launch the desktop GUI."""
    try:
        from documentcrawler.gui import launch
    except ImportError as e:
        console.print(f"[red]GUI unavailable: {e}[/red]")
        raise typer.Exit(1) from e
    launch(config_path)


@app.command()
def sources(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """List configured sources and whether each is enabled."""
    cfg, _ = _load(config_path)
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


if __name__ == "__main__":
    app()
