"""Unit tests for JSON log formatting."""

import json
import logging

from documentcrawler.utils.logging import JSONFormatter


def test_json_formatter_structure():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test log event %s",
        args=("hello",),
        exc_info=None,
    )
    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test_logger"
    assert parsed["message"] == "Test log event hello"
    assert "timestamp" in parsed
