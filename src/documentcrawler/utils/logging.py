"""Centralised logging configuration."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

from rich.logging import RichHandler

_CONFIGURED = False


class JSONFormatter(logging.Formatter):
    """Single-line JSON log formatter for cloud aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data)


def setup_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return

    use_json = json_format or os.getenv("LOG_FORMAT", "").lower() == "json"
    if use_json:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
    else:
        handler = RichHandler(rich_tracebacks=True, show_path=False, show_time=True)

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s" if not use_json else "%(message)s",
        handlers=[handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def write_unhandled(exc: BaseException) -> None:
    logging.exception("Unhandled error: %s", exc, file=sys.stderr)
