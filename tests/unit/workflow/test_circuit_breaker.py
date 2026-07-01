"""Unit tests for per-provider circuit breaker."""

import time

import pytest

from cloud_agents.workflow.circuit_breaker import ProviderCircuitBreaker


class TestProviderCircuitBreaker:
    """Tests for the ProviderCircuitBreaker class."""

    def test_closed_by_default(self) -> None:
        """New breaker is closed for any provider."""
        cb = ProviderCircuitBreaker()
        assert cb.is_open("openai") is False

    def test_opens_after_threshold_failures(self) -> None:
        """Breaker opens after reaching failure threshold."""
        cb = ProviderCircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("openai")
        assert cb.is_open("openai") is True

    def test_resets_after_timeout(self) -> None:
        """Breaker resets to closed after reset timeout elapses."""
        cb = ProviderCircuitBreaker(failure_threshold=2, reset_seconds=0.1)
        cb.record_failure("openai")
        cb.record_failure("openai")
        assert cb.is_open("openai") is True
        time.sleep(0.15)
        assert cb.is_open("openai") is False

    def test_success_resets_counter(self) -> None:
        """Recording a success resets the failure counter."""
        cb = ProviderCircuitBreaker(failure_threshold=3)
        cb.record_failure("openai")
        cb.record_failure("openai")
        cb.record_success("openai")
        cb.record_failure("openai")  # only 1 failure after reset
        assert cb.is_open("openai") is False

    def test_below_threshold_stays_closed(self) -> None:
        """Breaker stays closed when failures are below threshold."""
        cb = ProviderCircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure("openai")
        assert cb.is_open("openai") is False

    def test_providers_isolated(self) -> None:
        """Different providers have independent circuit breakers."""
        cb = ProviderCircuitBreaker(failure_threshold=2)
        cb.record_failure("openai")
        cb.record_failure("openai")
        assert cb.is_open("openai") is True
        assert cb.is_open("gemini") is False

    def test_one_provider_failure_does_not_affect_another(self) -> None:
        """Failures for one provider do not affect another's state."""
        cb = ProviderCircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("openai")
        cb.record_success("gemini")
        assert cb.is_open("openai") is True
        assert cb.is_open("gemini") is False
