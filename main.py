"""ASGI entrypoint for Vercel's FastAPI framework preset.

Deploys the documentcrawler server in public_demo mode: no background
worker (see server.py's lifespan), /acquire and /acquire/batch disabled
in favor of the synchronous /acquire/sync, and the six db-mutation
endpoints disabled outright. See api/vercel-config.toml.
"""

from pathlib import Path

from documentcrawler.server import create_app

app = create_app(Path(__file__).parent / "api" / "vercel-config.toml")
