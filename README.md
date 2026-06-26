# documentcrawler

A Python CLI that resolves a batch of document references (DOI, title/author/year,
ISBN, keywords) and tries to download the full text from a configurable list of
sources. Open-access sources (Unpaywall, OpenAlex, arXiv, PubMed Central, DOAJ)
are enabled by default; shadow-library sources (Sci-Hub, Anna's Archive, LibGen,
Z-Library) are off by default and must be opted into via `config.toml`.

## Features

- Batch import from CSV, BibTeX, RIS, or a plain DOI list.
- **Multi-source metadata search** (`documentcrawler search`, GUI **Search…**)
  across Crossref, OpenAlex, arXiv, Open Library, Semantic Scholar, Anna's
  Archive, LibGen, and Z-Library. Results are merged across sources,
  deduped by DOI / ISBN / title, and ranked by score and PDF availability.
- Metadata enrichment via Crossref, OpenAlex, and Unpaywall.
- Pluggable source registry with per-source enable / disable toggles and a
  configurable try-order.
- Async HTTP fetcher (`httpx`) with per-host rate limits, retries, and
  user-agent rotation; optional Playwright fallback for JS / Cloudflare pages.
- SQLite-backed job queue: every attempt is logged so runs are fully resumable
  (`run --only-failed`, `retry`).
- Atomic writes, PDF magic-byte verification, SHA-256 deduplication, and a
  configurable filename template.

## Install

```bash
git clone https://github.com/mpdecker/DogTheLibrarian.git
cd documentcrawler
pip install -e .
# optional: enable Playwright fallback for JS-heavy sources
pip install -e .[browser]
playwright install chromium
```

## Quick start

```bash
documentcrawler init --with-examples                # config.toml + crawler.db + ./examples/
documentcrawler add --doi 10.1038/s41586-020-2649-2 # queue one DOI
documentcrawler import examples/refs.bib            # or .csv .tsv .ris .txt
documentcrawler search "attention is all you need" --queue-top 3
documentcrawler run --workers 4
documentcrawler status
```

`init --with-examples` drops sample `refs.bib`, `dois.txt`, `library.csv`,
and `references.ris` files into `./examples/` so the import command
above works without you having to bring your own files. Replace them
with your own when you're ready. You can also re-emit the samples any
time with `documentcrawler examples ./my-samples`.

`search` queries any subset of metadata + library sources in parallel,
merges duplicates, and (optionally) enqueues hits into the queue with
`--queue-top N` or `--queue-all`.

`documentcrawler init` writes a `config.toml` you can edit to enable / disable
sources, set a Unpaywall email (required by their TOS), choose a download
directory, and tweak the filename template.

## Input formats

- **CSV**: any subset of columns `doi,title,authors,year,isbn,keywords,url`.
  `authors` may be `;`- or `&`-separated.
- **BibTeX**: standard `@article` / `@book` entries.
- **RIS**: TY/AU/TI/PY/DO tags.
- **DOI list**: one DOI per line (lines starting with `#` are ignored).

## Sources

| Source           | Default | Notes                                                |
| ---------------- | ------- | ---------------------------------------------------- |
| `open_access`    | on      | Unpaywall `best_oa_location` + OpenAlex `pdf_url`    |
| `arxiv`          | on      | arXiv API search by title/author                     |
| `pubmed`         | on      | NCBI ESearch / EFetch, PubMed Central OA full text   |
| `doaj`           | on      | DOAJ article search                                  |
| `scihub`         | off     | DOI-based, mirror auto-discovery                     |
| `annas_archive`  | off     | Search by title/ISBN/MD5                             |
| `libgen`         | off     | LibGen scimag (papers) and main library (books)      |
| `zlibrary`       | off     | Playwright + manual mirror list required             |

## GUI

```bash
documentcrawler gui
```

The GUI shares the same `config.toml` and SQLite database as the CLI, so
you can flip between the two freely. Tkinter ships with Python — no
extra install is required.

### Tour

```
+------------------------------------------------------------------+
|  Add  Search ▾  Import  |  Run  Cancel  ☐ only failed  ☐ legit |
+------------------------------------------------------------------+
|  Filter: [____________]   Status: [All ▾]   Clear   8 of 12 shown|
+------------------------------------------------------------------+
|                              |                                   |
|  Queue                       |  Detail panel                     |
|  ID  STATUS   DOI  TITLE …   |  Title (selected)                 |
|  ↑sortable, right-click for  |  DOI / ISBN / Year / Authors / … |
|   context menu               |  Open  Reveal  Open DOI  Edit ... |
|                              |                                   |
|  ----------------------------|  Attempts                         |
|  Log (capped, dark theme)    |  source ok status bytes when err  |
+------------------------------------------------------------------+
|  Queue: 12 total | done 4 | failed 1 | pending 7                 |
+------------------------------------------------------------------+
```

### Toolbar

- **Add** (Ctrl+N) — manually queue one document by DOI, title, ISBN, or URL.
- **Search ▾** (Ctrl+K) — multi-source metadata search across Crossref,
  OpenAlex, arXiv, Open Library, Semantic Scholar, Anna's Archive, and
  LibGen. Preview hits and queue selected ones.
- **Import file** (Ctrl+O) — pick a `.bib` / `.ris` / `.csv` / `.tsv` /
  `.txt` file and bulk-queue its references.
- **Run** (Ctrl+R / F5 refreshes) — start the download pipeline.
- **Cancel** (Ctrl+.) — interrupt an in-progress run. Already-finished
  documents stay done.
- **Only failed** — restrict the next run to documents currently in
  `failed` state.
- **Legit only** — exclude shadow-library sources for the next run.
- **Retry failed** — bulk-flip every failed document back to pending.
- **Refresh** — re-read the database (updates from CLI runs).
- **?** (F1) — About / shortcuts / tips.
- **Settings** (Ctrl+,) — see *Settings dialog* below.

### Filter bar

A live text filter (matches title, DOI, authors, year, ISBN, URL) plus a
status dropdown (All / Pending / In progress / Done / Failed). The
counter on the right shows `N of M shown`.

### Queue + detail panel

- Click a row to populate the right-hand **detail panel** with full
  metadata and the per-source attempt history (which source was tried,
  the HTTP status, byte count, and any error).
- **Double-click** opens the downloaded file (if there is one).
- **Delete** key (with confirmation) removes selected rows from the
  queue. Files on disk are kept.
- **Right-click** any row for a context menu:
  - Open file / Reveal in folder
  - Open DOI in browser / Copy DOI / Copy title
  - Edit metadata… (opens the edit dialog)
  - Re-queue (set pending — useful after enabling more sources)
  - Re-search this title (opens the Search dialog with the title or DOI
    pre-filled and runs immediately)
  - Delete from queue

### Search dialog

Open with **Search ▾** or `Ctrl+K`.

- Type a free-text query, DOI, ISBN, or author. Press **Enter** to
  search.
- Toggle individual searchers on the *Searchers* row, or use
  **OA only** / **All** for quick presets.
- Results are merged across sources, deduped by DOI / ISBN / title, and
  sorted by score and PDF availability. Rows with a known direct PDF
  link show in green.
- Click a result to preview its abstract.
- **Queue selected** queues the highlighted rows. **Queue all merged**
  queues every row in the table.
- **Cancel** stops a slow search mid-flight.

### Add / Edit dialogs

`Add` accepts DOI, title, authors (semicolon-separated), year, ISBN,
keywords, and URL. Provide at least one of DOI / title / ISBN / URL.

`Edit metadata…` (right-click → Edit, or `Ctrl+E`) lets you fix the
metadata of an existing document — useful when the auto-extracted DOI
or year is wrong.

### Settings dialog

Three tabs:

- **General** — download directory, SQLite path, worker count, filename
  / folder template, minimum PDF size, request timeout, log level.
- **Sources** — tick the sources you want enabled. Buttons to enable /
  disable all shadow libraries at once. The order of the rows is the
  fallback order for the run pipeline.
- **Metadata** — Unpaywall email (required by their TOS), Crossref and
  OpenAlex `mailto` (polite-pool routing), and an optional Semantic
  Scholar API key.

Settings are written back to `config.toml` immediately, so the CLI
picks them up too.

### Keyboard shortcuts

| Shortcut         | Action                                       |
| ---------------- | -------------------------------------------- |
| `Ctrl+N`         | Add document                                 |
| `Ctrl+O`         | Import file                                  |
| `Ctrl+K`         | Open search                                  |
| `Ctrl+F`         | Focus the queue filter                       |
| `Ctrl+R` / `F5`  | Run pipeline / Refresh queue                 |
| `Ctrl+.`         | Cancel a running pipeline                    |
| `Esc`            | Clear filter (in filter) / cancel run        |
| `Ctrl+E`         | Edit metadata of the selected document       |
| `Ctrl+,`         | Settings                                     |
| `Enter`          | Open file (in queue) / search (in dialog)    |
| `Delete`         | Delete from queue (with confirmation)        |
| `F1`             | Help / shortcuts                             |

### Troubleshooting search

If a particular source shows `0` hits in the *per-source* breakdown, the
**error** column tells you why:

- `rate limited` — the public Semantic Scholar / Crossref tier
  throttles unauthenticated traffic. Add an API key or `mailto`
  in `config.toml` (`[metadata.semantic_scholar].api_key`,
  `[metadata.crossref].mailto`) to lift the cap.
- `timeout` — the mirror didn't respond in time. Each shadow library
  mirror has its own `6 s` total / `3 s` connect budget, so dead hosts
  fail fast and the searcher falls through to the next mirror without
  retrying. Bump the global per-source budget with
  `documentcrawler search --timeout 60`.
- `getaddrinfo failed` / `connect error` — your DNS is blocking the
  domain. Try a different resolver (`1.1.1.1`, `8.8.8.8`) or update
  the `mirrors = [...]` list in `config.toml` for that source.
- `rate limited (429); set [metadata.semantic_scholar].api_key …` —
  Semantic Scholar throttles anonymous traffic to ~1 rps. The searcher
  honours `Retry-After` once and gives up cleanly so the rest of your
  results aren't blocked waiting on it.

The default mirror lists are tracked across releases; the searcher
augments any user mirror list with the latest known-good fallbacks
behind the scenes, so even a stale `config.toml` from an older release
will still keep working as long as one current mirror is reachable.
The default order of preferred mirrors is:

| Source         | Default order                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------------- |
| Anna's Archive | `annas-archive.org`, `annas-archive.gl`, `annas-archive.se`, `annas-archive.li`                    |
| LibGen         | `libgen.li`, `libgen.gs`, `libgen.is`, `libgen.rs`                                                 |
| Z-Library      | `z-lib.fm`, `z-library.sk`, `1lib.sk`, `z-lib.io`, `z-lib.gs`                                      |

If you upgraded from a pre-0.2 release, your existing `config.toml`
may still pin the old `.is`/`.rs` LibGen mirrors as the *first* entries
— harmless thanks to the augmentation logic, but slower. Delete or
re-order those lines (or run `documentcrawler init --force`) to put
the current defaults at the top.

### Troubleshooting downloads

- **HTML instead of a PDF** — Anna's Archive, Z-Library, LibGen mirrors, and
  Sci-Hub often return an HTML interstitial (mirror picker, countdown, or
  Cloudflare). The client now parses those pages for real PDF/download URLs
  over plain HTTP first, then uses Playwright when the `[browser]` extra is
  installed (`pip install -e .[browser]` and `playwright install chromium`).
- **`file too small` / verification** — The pipeline checks for a PDF header and
  a minimum size (`[general].min_pdf_bytes`, default 20480). Lower that value
  in `config.toml` if you hit false negatives on short papers.

### Safety + robustness

- Closing the window during a run prompts you to confirm and cancels
  cleanly.
- The pipeline always runs on a background thread — the UI never
  freezes, even on slow networks.
- A worker that crashes unexpectedly resets the toolbar instead of
  leaving the buttons locked.
- The log panel is capped (rolling buffer) so multi-thousand-document
  runs stay smooth.
- `Settings` is disabled while a run is in progress to prevent the
  config from being changed mid-flight.

## Legal note

This tool queries both legitimate open-access services and shadow libraries.
Sci-Hub, Anna's Archive, LibGen, and Z-Library host content under disputed
legal status in various jurisdictions. You are solely responsible for your
use of this tool, for complying with the terms of service of any source you
query, and with the copyright laws applicable in your country. The
maintainers do not host or distribute any content; this is a client-side
tool that follows publicly reachable URLs at the user's direction.

Use `documentcrawler run --legit-only` (or `documentcrawler search
--legit-only`, or untick the shadow sources in the GUI's Settings dialog)
to restrict yourself to legitimate open-access and catalog services.

## Development

```bash
pip install -e .[dev,browser]
pytest
ruff check src tests
```
