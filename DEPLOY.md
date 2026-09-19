# documentcrawler — deployment

_Last updated: 2026-09-19_

## Stack

- Python 3.11+ FastAPI app (`src/documentcrawler/server.py`), ASGI entry at
  `main.py` for Vercel's FastAPI framework preset.
- SQLite + downloaded PDFs land in `/tmp` on serverless (ephemeral by design);
  on a long-running host they use the paths in `config.toml`.

## Deployments

| Instance | URL | Mode |
| --- | --- | --- |
| Vercel public demo | https://documentcrawler.vercel.app | `public_demo` — see `api/vercel-config.toml` |
| Local full server | `documentcrawler serve` (default `127.0.0.1:8099`) | background worker, all endpoints |

`public_demo = true` disables `/acquire` + `/acquire/batch` (use
`/acquire/sync`), the `/db/*` mutation endpoints, and webhook registration.
Set `DOCUMENTCRAWLER_API_KEY` as a host env var to additionally require an
API key on every request.

## Vercel deploy

```bash
npm i -g vercel
vercel login
vercel link          # once per clone
vercel deploy --prod # remote build (Linux)
```

How the build works:

- Vercel auto-detects the FastAPI framework and mounts `main.py` as the
  catch-all ASGI function (`vercel.json` only sets `maxDuration: 30`).
- Dependencies install via **uv** from `pyproject.toml`. uv never installs
  *extras*, so the `serve` extra is mirrored as a `[dependency-groups]`
  entry listed in `[tool.uv] default-groups` — keep both in sync.
- `uv.lock` pins the resolved set; commit it and regenerate
  (`rm uv.lock && vercel build` locally) after changing dependencies.
  Local `vercel build` on Windows needs a `python3` shim on `PATH`.
- `.python-version` (3.12) pins the runtime.

## Local dev

```bash
pip install -e .[dev,serve]
python -m documentcrawler serve --port 8099   # needs config.toml (copy config.example.toml)
pytest                                        # add -m e2e for network tests
```

Note: `tests/test_config.py::test_config_reload_fresh_copy` reads an explicit
tmp config — a `config.toml` in the repo root (normal for local serving) used
to leak into it.

## Smoke check

- [ ] `GET /` in a browser shows the "Document Finder" UI; with `Accept: application/json` returns the JSON service description (`mode: public_demo` on the demo)
- [ ] `GET /health` → `{"status": "ok"}`
- [ ] `POST /acquire/sync` with a known OA DOI returns `status: done` and a
      `sha256`, with `enriched.enrich_providers` listing who answered
- [ ] `GET /documents/{id}/file` streams the PDF (410 with a friendly message
      once demo storage has recycled)
- [ ] `POST /acquire` on the demo → 400; `POST /webhooks` → 403
- [ ] Every error body uses the `{"error": {"code", "message"}}` envelope

## Rollback

`vercel` dashboard → Deployments → promote the previous build, or
`vercel --prod` from the previous commit.
