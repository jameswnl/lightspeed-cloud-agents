"""Per-provider circuit breaker for sandbox execution.

Tracks consecutive failures per LLM provider and opens the circuit
(fails fast) when a threshold is reached. Resets automatically after
a configurable timeout. Per-process only (no cross-replica state).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _ProviderState:
    """Internal state for a single provider's circuit breaker."""

    failures: int = 0
    last_failure: float = 0.0


class ProviderCircuitBreaker:
    """Per-provider circuit breaker.

    Tracks consecutive sandbox failures per provider name. When the
    failure count reaches the threshold the circuit opens, causing
    subsequent calls to fail fast until the reset timeout elapses.

    Per-process only -- no cross-replica state sharing.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_seconds: float = 60.0,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._providers: dict[str, _ProviderState] = {}

    def record_success(self, provider: str) -> None:
        """Record a successful call, resetting the failure counter."""
        if provider in self._providers:
            self._providers[provider].failures = 0

    def record_failure(self, provider: str) -> None:
        """Record a failed call, incrementing the failure counter."""
        state = self._providers.setdefault(provider, _ProviderState())
        state.failures += 1
        state.last_failure = time.monotonic()

    def is_open(self, provider: str) -> bool:
        """Check whether the circuit is open (should fail fast).

        Returns True if the provider has hit the failure threshold
        and the reset timeout has not yet elapsed.
        """
        state = self._providers.get(provider)
        if state is None or state.failures < self._threshold:
            return False
        if time.monotonic() - state.last_failure > self._reset_seconds:
            state.failures = 0
            return False
        return True
