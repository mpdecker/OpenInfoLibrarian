"""OpenAPI specification generator and exporter."""

from __future__ import annotations

import json
from pathlib import Path

from documentcrawler.server import create_app


def export_openapi_schema(config_path: Path | None = None, fmt: str = "json") -> str:
    """Generate and serialize the OpenAPI 3.0 specification schema for DocumentCrawler REST API."""
    from documentcrawler.cli import DEFAULT_CONFIG_PATH

    cfg = config_path or DEFAULT_CONFIG_PATH
    app = create_app(cfg)
    schema = app.openapi()

    if fmt.lower() in ("yaml", "yml"):
        try:
            import yaml
            return yaml.dump(schema, sort_keys=False)
        except ImportError:
            # Fallback to JSON if PyYAML is not installed
            return json.dumps(schema, indent=2)

    return json.dumps(schema, indent=2)
