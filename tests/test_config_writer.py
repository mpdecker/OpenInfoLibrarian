from pathlib import Path

from documentcrawler.config import (
    Config,
    GeneralConfig,
    SourceConfig,
    load_config,
)
from documentcrawler.config_writer import render_config_toml, write_config


def _sample_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.general = GeneralConfig(
        download_dir=tmp_path / "dl",
        db_path=tmp_path / "x.db",
        workers=8,
        filename_template="{doi_slug}.{ext}",
    )
    cfg.metadata = {
        "unpaywall": {"email": "me@example.com"},
        "crossref": {"mailto": "me@example.com"},
    }
    cfg.sources = {
        "open_access": SourceConfig(name="open_access", enabled=True),
        "scihub": SourceConfig(
            name="scihub",
            enabled=True,
            options={"mirrors": ["https://sci-hub.se", "https://sci-hub.ru"]},
        ),
        "zlibrary": SourceConfig(name="zlibrary", enabled=False, options={"mirrors": []}),
    }
    cfg.sources_order = ["open_access", "scihub"]
    return cfg


def test_render_round_trip(tmp_path: Path):
    cfg = _sample_cfg(tmp_path)
    text = render_config_toml(cfg)
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    loaded = load_config(path)
    assert loaded.general.workers == 8
    assert loaded.general.filename_template == "{doi_slug}.{ext}"
    assert loaded.sources_order == ["open_access", "scihub"]
    assert loaded.source("scihub").enabled is True
    assert loaded.source("scihub").options.get("mirrors") == [
        "https://sci-hub.se", "https://sci-hub.ru",
    ]
    assert loaded.source("zlibrary").enabled is False
    assert (loaded.metadata.get("unpaywall") or {}).get("email") == "me@example.com"


def test_write_config_creates_parent_dirs(tmp_path: Path):
    cfg = _sample_cfg(tmp_path)
    target = tmp_path / "nested" / "cfg" / "config.toml"
    write_config(cfg, target)
    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("# documentcrawler")
