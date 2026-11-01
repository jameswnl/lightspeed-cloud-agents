"""Tests for per-sandbox lifetime bounds used to stamp expires-at labels."""

from __future__ import annotations

from cloud_agents.spawner import lifetime
from cloud_agents.spawner.lifetime import run_timeout_seconds, step_sandbox_lifetime_seconds

# Enforced deadlines around the run itself: spawn budget + transcript fetch +
# transcript save, plus destroy grace.
STEP_OVERHEAD = 420 + 30 + 30 + 120


class TestRunTimeout:
    def test_default(self) -> None:
        assert run_timeout_seconds({"name": "s"}) == 600

    def test_permissions_timeout(self) -> None:
        assert run_timeout_seconds({"permissions": {"timeout_seconds": 5000}}) == 5000


class TestStepSandboxLifetime:
    """Lifetime = sum of the enforced deadlines a step's sandbox can live through."""

    def test_default_run_timeout(self) -> None:
        assert step_sandbox_lifetime_seconds({"name": "s"}) == 600 + STEP_OVERHEAD

    def test_follows_enforced_run_timeout(self) -> None:
        step = {"timeout_seconds": 100, "permissions": {"timeout_seconds": 200_000}}
        assert step_sandbox_lifetime_seconds(step) == 200_000 + STEP_OVERHEAD

    def test_rounds_fractional_run_timeout_up(self) -> None:
        step = {"permissions": {"timeout_seconds": 0.5}}
        assert step_sandbox_lifetime_seconds(step) == 1 + STEP_OVERHEAD

    def test_constants(self) -> None:
        """Spawn budget covers the 30s SDK RPC timeout for CreateSandbox, the
        300s gateway ready wait, the 60s HTTP readiness check and 30s of
        post-create exec/expose calls."""
        assert lifetime.SANDBOX_SPAWN_DEADLINE_SECONDS == 420
        assert lifetime.SANDBOX_SPAWN_DEADLINE_SECONDS >= (
            30
            + lifetime.SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS
            + lifetime.SANDBOX_HTTP_READY_TIMEOUT_SECONDS
        )
        assert lifetime.DEFAULT_STEP_TIMEOUT_SECONDS == 600
        assert lifetime.SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS == 30
        assert lifetime.SANDBOX_TRANSCRIPT_SAVE_TIMEOUT_SECONDS == 30
