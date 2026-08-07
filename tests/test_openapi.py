"""Unit tests for OpenAPI schema exporter."""

from __future__ import annotations

import json

from documentcrawler.openapi import export_openapi_schema


def test_export_openapi_schema_json(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\ndownload_dir = "{tmp_path.as_posix()}"\n'
        f'db_path = "{(tmp_path / "test.db").as_posix()}"\n'
    )

    spec_str = export_openapi_schema(config_path=config_path, fmt="json")
    spec = json.loads(spec_str)

    assert "openapi" in spec
    assert spec["openapi"].startswith("3.")
    assert "/queue" in spec["paths"]
    assert "/db/dedupe" in spec["paths"]
    assert "/db/clean" in spec["paths"]
