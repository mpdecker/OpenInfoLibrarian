"""TOML config loader with sensible fallbacks."""

from __future__ import annotations

import logging
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from documentcrawler.errors import ConfigError

log = logging.getLogger(__name__)

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
    "annas-archive.gl": 1.0,
    "annas-archive.se": 1.0,
    "libgen.is": 1.0,
    "libgen.rs": 1.0,
    "libgen.li": 1.0,
    "libgen.gs": 1.0,
    "api.semanticscholar.org": 0.5,
    "z-lib.fm": 1.0,
    "z-library.sk": 1.0,
    "1lib.sk": 1.0,
    "z-lib.io": 1.0,
    "z-lib.gs": 1.0,
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
    pipeline_timeout_s: int = 300
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

    def validate(self) -> list[str]:
        """Validate config values. Returns a list of warning strings.
        Raises ConfigError for fatal problems.
        """
        warnings: list[str] = []

        dl_dir = self.general.download_dir
        try:
            dl_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ConfigError(
                f"download_dir '{dl_dir}' is not creatable: {e}"
            ) from e
        if dl_dir.exists() and not os.access(dl_dir, os.W_OK):
            raise ConfigError(f"download_dir '{dl_dir}' is not writable")

        db_parent = self.general.db_path.parent
        try:
            db_parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ConfigError(
                f"db_path parent '{db_parent}' is not creatable: {e}"
            ) from e
        if db_parent.exists() and not os.access(db_parent, os.W_OK):
            raise ConfigError(f"db_path parent '{db_parent}' is not writable")

        if not (1 <= self.general.workers <= 16):
            warnings.append(
                f"workers={self.general.workers} is out of range [1, 16]; "
                f"clamped to {max(1, min(16, self.general.workers))}"
            )

        if not (1 <= self.general.request_timeout_s <= 300):
            warnings.append(
                f"request_timeout_s={self.general.request_timeout_s} is out of "
                f"range [1, 300]"
            )

        if not (0 <= self.general.max_retries <= 10):
            warnings.append(
                f"max_retries={self.general.max_retries} is out of range [0, 10]"
            )

        if not (1024 <= self.general.min_pdf_bytes <= 10_485_760):
            warnings.append(
                f"min_pdf_bytes={self.general.min_pdf_bytes} is out of range "
                f"[1024, 10MB]"
            )

        if not (30 <= self.general.pipeline_timeout_s <= 3600):
            warnings.append(
                f"pipeline_timeout_s={self.general.pipeline_timeout_s} is out of "
                f"range [30, 3600]"
            )

        if "{" not in self.general.filename_template:
            raise ConfigError(
                "filename_template must contain at least one {placeholder}"
            )

        for name in self.sources_order:
            if name not in self.sources:
                warnings.append(f"Source '{name}' in order list is not configured")

        return warnings

    @staticmethod
    def reload(path: Path | None = None) -> Config:
        """Re-read the TOML file and return a fresh Config."""
        return load_config(path)


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
    for key in ("workers", "request_timeout_s", "max_retries", "min_pdf_bytes", "pipeline_timeout_s"):
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
    warnings = config.validate()
    for w in warnings:
        log.warning("Config: %s", w)
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
folder_template   = "{first_author_initial}"
workers           = 4
request_timeout_s = 30
max_retries       = 3
min_pdf_bytes     = 20480
pipeline_timeout_s = 300
log_level         = "INFO"

[fetcher]
default_rate          = 2.0
"api.crossref.org"    = 50.0
"api.openalex.org"    = 10.0
"api.unpaywall.org"   = 10.0
"export.arxiv.org"    = 1.0
"eutils.ncbi.nlm.nih.gov" = 3.0
"sci-hub.se"          = 1.0
"sci-hub.ru"          = 1.0
"sci-hub.st"          = 1.0
"annas-archive.org"   = 1.0
"annas-archive.gl"    = 1.0
"annas-archive.se"    = 1.0
"libgen.is"           = 1.0
"libgen.rs"           = 1.0
"libgen.li"           = 1.0
"libgen.gs"           = 1.0
"api.semanticscholar.org" = 0.5
"z-lib.fm"            = 1.0
"z-library.sk"        = 1.0
"1lib.sk"             = 1.0
"z-lib.io"            = 1.0
"z-lib.gs"            = 1.0

[fetcher.user_agents]
list = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

[metadata.unpaywall]
email = "you@example.com"

[metadata.crossref]
mailto = "you@example.com"

[sources]
order = [
    "open_access", "arxiv", "pubmed", "doaj",
    "scihub", "annas_archive", "libgen", "zlibrary",
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
enabled = false
mirrors = ["https://sci-hub.se", "https://sci-hub.ru", "https://sci-hub.st", "https://sci-hub.ee"]
[sources.annas_archive]
enabled = false
mirrors = [
    "https://annas-archive.org",
    "https://annas-archive.gl",
    "https://annas-archive.se",
    "https://annas-archive.li",
]
base_url = "https://annas-archive.org"
[sources.libgen]
enabled = false
mirrors = [
    "https://libgen.li",
    "https://libgen.gs",
    "https://libgen.is",
    "https://libgen.rs",
]
[sources.zlibrary]
enabled = false
mirrors = [
    "https://z-lib.fm",
    "https://z-library.sk",
    "https://1lib.sk",
    "https://z-lib.io",
    "https://z-lib.gs",
]
"""
