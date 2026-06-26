# Contributing

## Setup

```bash
git clone https://github.com/mpdecker/DogTheLibrarian.git
cd documentcrawler
pip install -e .[dev,browser]
playwright install chromium
```

## Tests

```powershell
# Windows
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest tests/ -m "not e2e" -p asyncio -p pytest_httpx
```

```bash
# Linux / macOS
python -m pytest tests/ -m "not e2e" -p asyncio -p pytest_httpx
```

Note: On Windows, `langsmith` (a transitive dependency) may crash pytest during plugin collection. Setting `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` works around this.

## Lint

```bash
ruff check src tests
```

## PR Expectations

- Run tests and lint locally before opening a PR.
- Describe what changed and why.
- Keep changes focused — one concern per PR.
