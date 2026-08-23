"""Tests for WORKFLOW_ENGINE switch in entrypoint."""

from __future__ import annotations

import os

import pytest
from pytest_mock import MockerFixture


class TestWorkflowEngineSwitch:
    """Tests for WORKFLOW_ENGINE env var routing."""

    def test_local_engine_creates_local_runner(
        self, mocker: MockerFixture
    ) -> None:
        """WORKFLOW_ENGINE=local creates a LocalWorkflowRunner."""
        mocker.patch.dict(os.environ, {"WORKFLOW_ENGINE": "local"}, clear=False)

        from cloud_agents.workflow.executor.factory import create_runner

        runner = create_runner()
        from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

        assert isinstance(runner, LocalWorkflowRunner)

    def test_default_engine_is_local(self, mocker: MockerFixture) -> None:
        """Default WORKFLOW_ENGINE is local."""
        mocker.patch.dict(os.environ, {}, clear=False)
        os.environ.pop("WORKFLOW_ENGINE", None)

        from cloud_agents.workflow.executor.factory import create_runner

        runner = create_runner()
        from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

        assert isinstance(runner, LocalWorkflowRunner)

    def test_temporal_without_url_raises(self, mocker: MockerFixture) -> None:
        """WORKFLOW_ENGINE=temporal without TEMPORAL_URL raises."""
        mocker.patch.dict(
            os.environ,
            {"WORKFLOW_ENGINE": "temporal", "TEMPORAL_URL": ""},
            clear=False,
        )

        from cloud_agents.workflow.executor.factory import create_runner

        with pytest.raises(ValueError, match="TEMPORAL_URL"):
            create_runner()

    def test_alert_trigger_under_local_raises(
        self, mocker: MockerFixture
    ) -> None:
        """ALERT_TRIGGER_ENABLED=true under local engine raises."""
        mocker.patch.dict(
            os.environ,
            {"WORKFLOW_ENGINE": "local", "ALERT_TRIGGER_ENABLED": "true"},
            clear=False,
        )

        from cloud_agents.workflow.executor.factory import create_runner

        with pytest.raises(ValueError, match="ALERT_TRIGGER"):
            create_runner()

    def test_schedule_under_local_raises(
        self, mocker: MockerFixture
    ) -> None:
        """SCHEDULE_* config under local engine raises."""
        mocker.patch.dict(
            os.environ,
            {"WORKFLOW_ENGINE": "local", "SCHEDULE_ENABLED": "true"},
            clear=False,
        )

        from cloud_agents.workflow.executor.factory import create_runner

        with pytest.raises(ValueError, match="SCHEDULE"):
            create_runner()

    def test_unknown_engine_raises(self, mocker: MockerFixture) -> None:
        """Unknown WORKFLOW_ENGINE value raises."""
        mocker.patch.dict(
            os.environ, {"WORKFLOW_ENGINE": "unknown"}, clear=False
        )

        from cloud_agents.workflow.executor.factory import create_runner

        with pytest.raises(ValueError, match="unknown"):
            create_runner()

    def test_no_temporal_imports_in_factory(self) -> None:
        """Factory module has zero temporalio imports."""
        from cloud_agents.workflow.executor import factory as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
