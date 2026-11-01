"""Tests for per-sandbox lifetime bounds used to stamp expires-at labels."""

from __future__ import annotations

from cloud_agents.spawner import lifetime
from cloud_agents.spawner.lifetime import (
    cli_session_lifetime_seconds,
    step_sandbox_lifetime_seconds,
)

# Everything around the step's own run time: gateway ready wait + HTTP ready
# wait + Temporal heartbeat timeout + transcript collection + destroy grace.
STEP_OVERHEAD = 300 + 60 + 180 + 30 + 120


class TestStepSandboxLifetime:
    def test_default_step_timeout(self) -> None:
        assert step_sandbox_lifetime_seconds({"name": "s"}) == 600 + STEP_OVERHEAD

    def test_step_timeout_drives_lifetime(self) -> None:
        assert step_sandbox_lifetime_seconds({"timeout_seconds": 200_000}) == (
            200_000 + STEP_OVERHEAD
        )

    def test_permissions_timeout_wins_when_longer(self) -> None:
        step = {"timeout_seconds": 100, "permissions": {"timeout_seconds": 5000}}
        assert step_sandbox_lifetime_seconds(step) == 5000 + STEP_OVERHEAD

    def test_step_timeout_wins_when_longer(self) -> None:
        step = {"timeout_seconds": 9000, "permissions": {"timeout_seconds": 50}}
        assert step_sandbox_lifetime_seconds(step) == 9000 + STEP_OVERHEAD

    def test_constants_match_their_sources(self) -> None:
        """The margins mirror the real waits/timeouts they bound."""
        assert lifetime.DEFAULT_STEP_TIMEOUT_SECONDS == 600
        assert lifetime.SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS == 300
        assert lifetime.SANDBOX_HTTP_READY_TIMEOUT_SECONDS == 60
        assert lifetime.SANDBOX_ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS == 180
        assert lifetime.SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS == 30


class TestCliSessionLifetime:
    def test_follows_max_session_seconds(self) -> None:
        assert cli_session_lifetime_seconds(3600, 30.0) == 3600 + 30 + 300 + 60 + 120
        assert cli_session_lifetime_seconds(7200, 30.0) == 7200 + 30 + 300 + 60 + 120

    def test_rounds_check_interval_up(self) -> None:
        assert cli_session_lifetime_seconds(100, 0.5) == 100 + 1 + 300 + 60 + 120
