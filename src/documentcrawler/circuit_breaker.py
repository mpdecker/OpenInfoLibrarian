"""Upstream source health and rate-limiting circuit breaker."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SourceState:
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    tripped_until: float = 0.0


class CircuitBreaker:
    """Manages cooldown state for sources experiencing rate limits or failures."""

    def __init__(self, max_consecutive_failures: int = 3, cooldown_seconds: float = 60.0):
        self.max_consecutive_failures = max_consecutive_failures
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, SourceState] = {}

    def is_open(self, source_name: str) -> bool:
        """Return True if the circuit breaker is open (tripped) and the source is currently cooling down."""
        state = self._states.get(source_name)
        if not state:
            return False
        if state.tripped_until > time.monotonic():
            return True
        return False

    def record_success(self, source_name: str) -> None:
        """Record a successful source attempt and reset consecutive failures."""
        state = self._states.setdefault(source_name, SourceState())
        state.consecutive_failures = 0
        state.total_successes += 1
        state.tripped_until = 0.0

    def record_failure(self, source_name: str, is_rate_limit: bool = False) -> bool:
        """Record a failure for the source. Returns True if circuit breaker was tripped."""
        state = self._states.setdefault(source_name, SourceState())
        state.consecutive_failures += 1
        state.total_failures += 1

        if is_rate_limit or state.consecutive_failures >= self.max_consecutive_failures:
            state.tripped_until = time.monotonic() + self.cooldown_seconds
            return True
        return False

    def get_status(self, source_name: str) -> dict[str, Any]:
        """Return circuit breaker metrics for a given source."""
        state = self._states.get(source_name, SourceState())
        is_tripped = self.is_open(source_name)
        cooldown_remaining = max(0.0, round(state.tripped_until - time.monotonic(), 1)) if is_tripped else 0.0
        return {
            "source": source_name,
            "is_tripped": is_tripped,
            "cooldown_remaining_s": cooldown_remaining,
            "consecutive_failures": state.consecutive_failures,
            "total_failures": state.total_failures,
            "total_successes": state.total_successes,
        }
