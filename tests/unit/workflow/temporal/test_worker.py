"""Unit tests for Temporal worker setup (TDD)."""

from __future__ import annotations

from cloud_agents.workflow.temporal_worker import build_worker_config


class TestWorkerConfig:
    """Tests for worker configuration."""

    def test_default_config(self) -> None:
        """Default config uses standard task queue and concurrency."""
        config = build_worker_config()
        assert config.task_queue == "cloud-agents"
        assert config.max_concurrent_activities == 10

    def test_custom_task_queue(self) -> None:
        """Task queue can be overridden."""
        config = build_worker_config(task_queue="custom-q")
        assert config.task_queue == "custom-q"

    def test_custom_concurrency(self) -> None:
        """Max concurrent activities can be overridden."""
        config = build_worker_config(max_concurrent_activities=5)
        assert config.max_concurrent_activities == 5

    def test_config_has_workflows_and_activities(self) -> None:
        """Config includes the registered workflows and activities."""
        config = build_worker_config()
        assert len(config.workflows) == 1
        assert len(config.activities) >= 2
