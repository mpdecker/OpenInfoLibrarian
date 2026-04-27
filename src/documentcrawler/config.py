"""TOML config loader with sensible fallbacks."""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.toml")
EXAMPLE_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.example.toml"


_DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

_DEFAULT_RATE_LIMITS: dict[str, float] = {
    "default_rate": 2.0,
    "api.crossref.org": 50.0,
    "api.openalex.org": 10.0,
    "api.unpaywall.org": 10.0,
    "export.arxiv.org": 1.0,
    "eutils.ncbi.nlm.nih.gov": 3.0,
    "sci-hub.se": 1.0,
    "sci-hub.ru": 1.0,
    "sci-hub.st": 1.0,
    "annas-archive.org": 1.0,
    "libgen.is": 1.0,
    "libgen.rs": 1.0,
}


@dataclass
class GeneralConfig:
    download_dir: Path = Path("./downloads")
    db_path: Path = Path("./crawler.db")
    filename_template: str = "{first_author_last}_{year}_{title_slug}.{ext}"
    folder_template: str = "{first_author_initial}"
    workers: int = 4
    request_timeout_s: int = 30
    max_retries: int = 3
    min_pdf_bytes: int = 20480
    log_level: str = "INFO"


@dataclass
class FetcherConfig:
    rate_limits: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_RATE_LIMITS))
    user_agents: list[str] = field(default_factory=lambda: list(_DEFAULT_USER_AGENTS))


@dataclass
class SourceConfig:
    name: str
    enabled: bool = False
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    fetcher: FetcherConfig = field(default_factory=FetcherConfig)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources_order: list[str] = field(default_factory=list)
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name, SourceConfig(name=name, enabled=False))

    def enabled_sources_in_order(self, override: list[str] | None = None) -> list[str]:
        """Return source names in try-order, filtered by enabled flag."""
        order = override if override else self.sources_order
        return [n for n in order if self.source(n).enabled]


def _coerce_general(raw: dict[str, Any]) -> GeneralConfig:
    cfg = GeneralConfig()
    if "download_dir" in raw:
        cfg.download_dir = Path(raw["download_dir"])
    if "db_path" in raw:
        cfg.db_path = Path(raw["db_path"])
    for key in (
        "filename_template",
        "folder_template",
        "log_level",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])
    for key in ("workers", "request_timeout_s", "max_retries", "min_pdf_bytes"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    return cfg


def _coerce_fetcher(raw: dict[str, Any]) -> FetcherConfig:
    cfg = FetcherConfig()
    rates: dict[str, float] = dict(_DEFAULT_RATE_LIMITS)
    for key, value in raw.items():
        if key == "user_agents":
            continue
        if isinstance(value, (int, float)):
            rates[key] = float(value)
    cfg.rate_limits = rates
    ua = raw.get("user_agents", {})
    if isinstance(ua, dict) and isinstance(ua.get("list"), list):
        cfg.user_agents = list(ua["list"])
    elif isinstance(ua, list):
        cfg.user_agents = list(ua)
    return cfg


def _coerce_sources(raw: dict[str, Any]) -> tuple[list[str], dict[str, SourceConfig]]:
    order: list[str] = list(raw.get("order", []))
    sources: dict[str, SourceConfig] = {}
    for key, value in raw.items():
        if key == "order":
            continue
        if not isinstance(value, dict):
            continue
        enabled = bool(value.get("enabled", False))
        options = {k: v for k, v in value.items() if k != "enabled"}
        sources[key] = SourceConfig(name=key, enabled=enabled, options=options)
    return order, sources


def load_config(path: Path | None = None) -> Config:
    """Load config.toml from the given path (default: ./config.toml).

    Missing files yield a default Config with no sources enabled.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return Config()

    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    config = Config(raw=raw)
    config.general = _coerce_general(raw.get("general", {}))
    config.fetcher = _coerce_fetcher(raw.get("fetcher", {}))
    config.metadata = raw.get("metadata", {})
    config.sources_order, config.sources = _coerce_sources(raw.get("sources", {}))
    return config


def write_default_config(target: Path = DEFAULT_CONFIG_PATH) -> Path:
    """Copy the bundled example config into `target` if it does not exist."""
    target = Path(target)
    if target.exists():
        return target
    if EXAMPLE_CONFIG_PATH.exists():
        shutil.copy(EXAMPLE_CONFIG_PATH, target)
    else:
        target.write_text(_FALLBACK_CONFIG, encoding="utf-8")
    return target


_FALLBACK_CONFIG = """\
[general]
download_dir      = "./downloads"
db_path           = "./crawler.db"
filename_template = "{first_author_last}_{year}_{title_slug}.{ext}"
workers           = 4

[metadata.unpaywall]
email = "you@example.com"

[sources]
order = [
    "open_access", "arxiv", "pubmed", "doaj",
    "scihub", "annas_archive", "libgen",
]

[sources.open_access]
enabled = true
[sources.arxiv]
enabled = true
[sources.pubmed]
enabled = true
[sources.doaj]
enabled = true
[sources.scihub]
enabled = true
mirrors = ["https://sci-hub.se", "https://sci-hub.ru", "https://sci-hub.st", "https://sci-hub.ee"]
[sources.annas_archive]
enabled = true
# `mirrors` is tried in order; the legacy `base_url` is appended for back-compat.
mirrors = [
    "https://annas-archive.org",
    "https://annas-archive.gl",
    "https://annas-archive.se",
    "https://annas-archive.li",
]
base_url = "https://annas-archive.org"
[sources.libgen]
enabled = true
# libgen.li / libgen.gs are the most reliably reachable mirrors today.
# .is / .rs are kept as fall-backs but are frequently DNS-blocked.
mirrors = [
    "https://libgen.li",
    "https://libgen.gs",
    "https://libgen.is",
    "https://libgen.rs",
]
[sources.zlibrary]
enabled = true
# z-lib.fm is the most reliably reachable clear-net mirror today; the
# others rotate often, so listing several gives the best fall-through.
mirrors = [
    "https://z-lib.fm",
    "https://z-library.sk",
    "https://1lib.sk",
    "https://z-lib.io",
    "https://z-lib.gs",
]
"""
