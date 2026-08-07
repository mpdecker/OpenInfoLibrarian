"""Unit tests for Source CircuitBreaker."""

from __future__ import annotations

import time

from documentcrawler.circuit_breaker import CircuitBreaker


def test_circuit_breaker_failures_and_cooldown():
    cb = CircuitBreaker(max_consecutive_failures=2, cooldown_seconds=0.5)
    assert cb.is_open("arxiv") is False

    cb.record_failure("arxiv")
    assert cb.is_open("arxiv") is False

    tripped = cb.record_failure("arxiv")
    assert tripped is True
    assert cb.is_open("arxiv") is True

    status = cb.get_status("arxiv")
    assert status["is_tripped"] is True
    assert status["total_failures"] == 2

    # Wait for cooldown
    time.sleep(0.55)
    assert cb.is_open("arxiv") is False


def test_circuit_breaker_rate_limit_immediate_trip():
    cb = CircuitBreaker(max_consecutive_failures=5, cooldown_seconds=10.0)
    cb.record_failure("crossref", is_rate_limit=True)
    assert cb.is_open("crossref") is True


def test_circuit_breaker_success_resets():
    cb = CircuitBreaker(max_consecutive_failures=3, cooldown_seconds=10.0)
    cb.record_failure("pubmed")
    cb.record_failure("pubmed")
    assert cb.is_open("pubmed") is False

    cb.record_success("pubmed")
    status = cb.get_status("pubmed")
    assert status["consecutive_failures"] == 0
    assert status["total_successes"] == 1
