"""Unit tests for schedule trigger — cron-based workflow execution (TDD).

Tests cover: schedule model validation, cron expression validation,
CRUD endpoints, entrypoint wiring, Prometheus metrics, authorization
actions, audit event types, RBAC enforcement, and content policy.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_stored_definition(name: str = "nightly-report") -> Any:
    """Create a mock StoredDefinition for testing."""
    from cloud_agents.workflow.definition import WorkflowDefinition

    defn = WorkflowDefinition.model_validate(
        {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": name},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "output_key": "r1",
                        "prompt": "generate report",
                    }
                ]
            },
            "provider": {
                "name": "openai",
                "model": "gpt-4",
                "credentials_secret": "test-key",
            },
        }
    )
    stored = MagicMock()
    stored.name = name
    stored.version = 1
    stored.definition = defn
    return stored


def _build_schedule_app(
    mock_temporal: Any,
    definition_store: Any = None,
    auth_dependency: Any = None,
    authorizer: Any = None,
    content_policy: Any = None,
) -> FastAPI:
    """Build a FastAPI app with the schedule router for testing."""
    from cloud_agents.workflow.definition_store import DefinitionStore
    from cloud_agents.workflow.schedule_trigger import build_schedule_router

    store = definition_store or DefinitionStore()
    app = FastAPI()
    router = build_schedule_router(
        temporal_client=mock_temporal,
        definition_store=store,
        auth_dependency=auth_dependency,
        authorizer=authorizer,
        content_policy=content_policy,
    )
    app.include_router(router)
    return app


SAMPLE_SCHEDULE_INPUT: dict[str, Any] = {
    "workflow_name": "nightly-report",
    "schedule": {
        "cron": "0 2 * * *",
    },
    "provider": {
        "name": "openai",
        "model": "gpt-4",
        "credentials_secret": "test-key",
    },
}


# ---------------------------------------------------------------------------
# Async iteration helper
# ---------------------------------------------------------------------------


def _async_iter(items: list) -> Any:
    """Create a mock async iterator from a list."""

    class _MockAsyncIter:
        def __init__(self, data: list) -> None:
            self._data = data
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._data):
                raise StopAsyncIteration
            item = self._data[self._index]
            self._index += 1
            return item

    return _MockAsyncIter(items)


# ===========================================================================
# 1. Pydantic model validation
# ===========================================================================


class TestScheduleModels:
    """Tests for schedule Pydantic models."""

    def test_valid_schedule_spec(self) -> None:
        """Valid ScheduleSpec with defaults."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="0 */6 * * *")
        assert spec.cron == "0 */6 * * *"
        assert spec.timezone == "UTC"
        assert spec.jitter_seconds == 0
        assert spec.overlap_policy == "skip"

    def test_schedule_spec_custom_values(self) -> None:
        """ScheduleSpec with custom values."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(
            cron="0 0 * * 1",
            timezone="America/New_York",
            jitter_seconds=60,
            overlap_policy="buffer_one",
        )
        assert spec.timezone == "America/New_York"
        assert spec.jitter_seconds == 60
        assert spec.overlap_policy == "buffer_one"

    def test_schedule_input_defaults(self) -> None:
        """ScheduleInput generates schedule_id when not provided."""
        from cloud_agents.workflow.schedule_trigger import ScheduleInput
        from cloud_agents.workflow.temporal_models import ProviderConfig

        inp = ScheduleInput(
            workflow_name="test-wf",
            schedule={"cron": "0 0 * * *"},
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
        )
        assert inp.schedule_id is not None
        assert inp.schedule_id.startswith("sched-")
        assert inp.paused is False
        assert inp.input_prompt is None

    def test_schedule_input_explicit_id(self) -> None:
        """ScheduleInput uses provided schedule_id."""
        from cloud_agents.workflow.schedule_trigger import ScheduleInput
        from cloud_agents.workflow.temporal_models import ProviderConfig

        inp = ScheduleInput(
            schedule_id="my-schedule",
            workflow_name="test-wf",
            schedule={"cron": "0 0 * * *"},
            provider=ProviderConfig(
                name="openai", model="gpt-4", credentials_secret="k"
            ),
        )
        assert inp.schedule_id == "my-schedule"

    def test_schedule_info_model(self) -> None:
        """ScheduleInfo holds schedule details."""
        from cloud_agents.workflow.schedule_trigger import ScheduleInfo

        info = ScheduleInfo(
            schedule_id="sched-1",
            workflow_name="test",
            cron="0 0 * * *",
            timezone="UTC",
            paused=False,
            overlap_policy="skip",
        )
        assert info.schedule_id == "sched-1"
        assert info.next_run is None
        assert info.last_run is None


# ===========================================================================
# 2. Cron expression validation
# ===========================================================================


class TestCronValidation:
    """Tests for cron expression validation."""

    def test_valid_standard_cron(self) -> None:
        """Standard 5-field cron expression accepted."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="0 */6 * * *")
        assert spec.cron == "0 */6 * * *"

    def test_valid_at_daily(self) -> None:
        """Temporal-supported @daily shorthand accepted."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="@daily")
        assert spec.cron == "@daily"

    def test_valid_at_hourly(self) -> None:
        """Temporal-supported @hourly shorthand accepted."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="@hourly")
        assert spec.cron == "@hourly"

    def test_valid_at_every_with_interval(self) -> None:
        """@every with interval argument accepted."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="@every 5m")
        assert spec.cron == "@every 5m"

    def test_bare_at_every_rejected(self) -> None:
        """Bare @every without interval argument rejected."""
        from pydantic import ValidationError

        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        with pytest.raises(ValidationError, match="@every requires an interval"):
            ScheduleSpec(cron="@every")

    def test_empty_cron_rejected(self) -> None:
        """Empty string cron expression rejected."""
        from pydantic import ValidationError

        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        with pytest.raises(ValidationError, match="cron"):
            ScheduleSpec(cron="")

    def test_garbage_cron_rejected(self) -> None:
        """Garbage string rejected."""
        from pydantic import ValidationError

        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        with pytest.raises(ValidationError, match="cron"):
            ScheduleSpec(cron="not a cron")

    def test_feb_30_accepted(self) -> None:
        """Feb 30 edge case accepted --- let Temporal handle it."""
        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        spec = ScheduleSpec(cron="0 0 30 2 *")
        assert spec.cron == "0 0 30 2 *"

    def test_six_field_cron_rejected(self) -> None:
        """Six-field (seconds) cron rejected --- only 5-field supported."""
        from pydantic import ValidationError

        from cloud_agents.workflow.schedule_trigger import ScheduleSpec

        with pytest.raises(ValidationError, match="cron"):
            ScheduleSpec(cron="0 0 0 * * *")


# ===========================================================================
# 3. CRUD endpoints
# ===========================================================================


class TestScheduleCreateEndpoint:
    """Tests for POST /v1/schedules."""

    def test_create_schedule_success(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules creates a Temporal schedule."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 201
        body = response.json()
        assert "schedule_id" in body
        mock_temporal.create_schedule.assert_called_once()

    def test_create_with_unknown_workflow_returns_404(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST with unknown workflow_name returns 404."""
        mock_temporal = mocker.MagicMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=None)

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 404

    def test_create_with_invalid_cron_returns_422(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST with invalid cron returns 422."""
        mock_temporal = mocker.MagicMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        bad_input = {
            **SAMPLE_SCHEDULE_INPUT,
            "schedule": {"cron": "not a cron"},
        }
        response = client.post("/v1/schedules", json=bad_input)
        assert response.status_code == 422

    def test_create_duplicate_schedule_returns_409(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST with duplicate schedule_id returns 409."""
        from temporalio.service import RPCError, RPCStatusCode

        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock(
            side_effect=RPCError(
                message="already exists",
                status=RPCStatusCode.ALREADY_EXISTS,
                raw_grpc_status=6,
            )
        )

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        input_with_id = {
            **SAMPLE_SCHEDULE_INPUT,
            "schedule_id": "my-schedule",
        }

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=input_with_id)
        assert response.status_code == 409

    def test_create_emits_audit_event(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST emits schedule_created audit event."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mock_emit = mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)

        created_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "schedule_created"
        ]
        assert len(created_calls) == 1
        assert created_calls[0][1]["details"]["workflow_name"] == "nightly-report"

    def test_create_emits_schedule_triggered_audit_event(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST emits schedule_triggered audit event on creation."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mock_emit = mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)

        triggered_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "schedule_triggered"
        ]
        assert len(triggered_calls) == 1
        assert triggered_calls[0][1]["details"]["trigger"] == "schedule_registered"

    def test_create_with_no_provider_in_definition_returns_400(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST when definition has no provider and none provided returns 400."""
        mock_temporal = mocker.MagicMock()

        stored = _make_stored_definition()
        stored.definition.provider = None

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=stored)

        # Input without provider
        no_provider_input = {
            "workflow_name": "nightly-report",
            "schedule": {"cron": "0 2 * * *"},
        }

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=no_provider_input)
        assert response.status_code == 400

    def test_create_workflow_id_not_static_placeholder(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Workflow ID in schedule action is not a static placeholder."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)

        call_kwargs = mock_temporal.create_schedule.call_args
        schedule_obj = call_kwargs.kwargs.get("schedule") or call_kwargs[1].get("schedule")
        action = schedule_obj.action
        # The workflow ID template should use Temporal's Go template syntax
        assert "{{.Now}}" in action.id
        assert "placeholder" not in action.id


class TestScheduleListEndpoint:
    """Tests for GET /v1/schedules."""

    def test_list_schedules_empty(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /v1/schedules returns empty list when no schedules."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.list_schedules = mocker.AsyncMock(return_value=_async_iter([]))

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/schedules")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_schedules_returns_entries(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /v1/schedules returns schedule list."""
        mock_entry = mocker.MagicMock()
        mock_entry.id = "sched-abc"
        mock_entry.memo = mocker.AsyncMock(
            return_value={"workflow_name": "nightly-report", "cron": "0 2 * * *"}
        )
        mock_entry.spec.spec.cron_expressions = ["0 2 * * *"]
        mock_entry.spec.spec.timezone_name = "UTC"
        mock_entry.state.paused = False
        mock_entry.info.next_action_times = []
        mock_entry.info.recent_actions = []
        mock_entry.policy.overlap = mocker.MagicMock()
        mock_entry.policy.overlap.name = "SKIP"

        mock_temporal = mocker.MagicMock()
        mock_temporal.list_schedules = mocker.AsyncMock(
            return_value=_async_iter([mock_entry])
        )

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/schedules")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["schedule_id"] == "sched-abc"


class TestScheduleGetEndpoint:
    """Tests for GET /v1/schedules/{schedule_id}."""

    def test_get_schedule_success(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /v1/schedules/{id} returns schedule details."""
        mock_handle = mocker.AsyncMock()
        desc = mocker.MagicMock()
        desc.id = "sched-abc"
        desc.memo = mocker.AsyncMock(
            return_value={"workflow_name": "nightly-report", "cron": "0 2 * * *"}
        )
        desc.schedule.spec.cron_expressions = ["0 2 * * *"]
        desc.schedule.spec.timezone_name = "UTC"
        desc.schedule.state.paused = False
        desc.info.next_action_times = []
        desc.info.recent_actions = []
        desc.schedule.policy.overlap = mocker.MagicMock()
        desc.schedule.policy.overlap.name = "SKIP"
        mock_handle.describe = mocker.AsyncMock(return_value=desc)

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/schedules/sched-abc")
        assert response.status_code == 200
        body = response.json()
        assert body["schedule_id"] == "sched-abc"
        assert body["cron"] == "0 2 * * *"


class TestScheduleDeleteEndpoint:
    """Tests for DELETE /v1/schedules/{schedule_id}."""

    def test_delete_schedule_success(
        self,
        mocker: MockerFixture,
    ) -> None:
        """DELETE /v1/schedules/{id} deletes the schedule."""
        mock_handle = mocker.AsyncMock()
        mock_handle.delete = mocker.AsyncMock()

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/v1/schedules/sched-abc")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
        mock_handle.delete.assert_called_once()

    def test_delete_emits_audit_event(
        self,
        mocker: MockerFixture,
    ) -> None:
        """DELETE emits schedule_deleted audit event."""
        mock_handle = mocker.AsyncMock()
        mock_handle.delete = mocker.AsyncMock()

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        mock_emit = mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        client.delete("/v1/schedules/sched-abc")

        deleted_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "schedule_deleted"
        ]
        assert len(deleted_calls) == 1
        assert deleted_calls[0][1]["details"]["schedule_id"] == "sched-abc"

    def test_delete_not_found_returns_404(
        self,
        mocker: MockerFixture,
    ) -> None:
        """DELETE for non-existent schedule returns 404."""
        from temporalio.service import RPCError, RPCStatusCode

        mock_handle = mocker.AsyncMock()
        mock_handle.delete = mocker.AsyncMock(
            side_effect=RPCError(
                message="not found",
                status=RPCStatusCode.NOT_FOUND,
                raw_grpc_status=5,
            )
        )

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/v1/schedules/nonexistent")
        assert response.status_code == 404


class TestSchedulePauseResumeEndpoints:
    """Tests for POST /v1/schedules/{id}/pause and /resume."""

    def test_pause_schedule(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules/{id}/pause pauses the schedule."""
        mock_handle = mocker.AsyncMock()
        mock_handle.pause = mocker.AsyncMock()

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules/sched-abc/pause")
        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_handle.pause.assert_called_once()

    def test_resume_schedule(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules/{id}/resume resumes the schedule."""
        mock_handle = mocker.AsyncMock()
        mock_handle.unpause = mocker.AsyncMock()

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules/sched-abc/resume")
        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_handle.unpause.assert_called_once()


class TestScheduleAuth:
    """Tests for auth enforcement on schedule endpoints."""

    def test_auth_enforced_when_configured(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Auth required when auth_dependency is set."""

        def reject_unauthenticated():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        mock_temporal = mocker.MagicMock()
        app = _build_schedule_app(
            mock_temporal,
            auth_dependency=reject_unauthenticated,
        )
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 401


# ===========================================================================
# 4. Entrypoint wiring
# ===========================================================================


class TestScheduleTriggerEntrypointWiring:
    """Tests for schedule trigger config wiring in entrypoint."""

    def test_schedule_endpoint_not_registered_when_disabled(
        self,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Endpoint not registered when SCHEDULE_TRIGGER_ENABLED=false."""
        monkeypatch.setenv("SCHEDULE_TRIGGER_ENABLED", "false")

        import importlib

        import cloud_agents.workflow.temporal_entrypoint as ep_mod

        importlib.reload(ep_mod)

        app = ep_mod.build_temporal_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        # Should get 404 because the route is not registered
        assert response.status_code in (404, 405)

    def test_schedule_endpoint_registered_when_enabled(
        self,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Endpoint registered when SCHEDULE_TRIGGER_ENABLED=true."""
        monkeypatch.setenv("SCHEDULE_TRIGGER_ENABLED", "true")

        import importlib

        import cloud_agents.workflow.temporal_entrypoint as ep_mod

        importlib.reload(ep_mod)

        app = ep_mod.build_temporal_app()

        # Verify endpoint is reachable (not the generic "Not Found" 404).
        # The endpoint returns its own 404 when the workflow definition
        # is missing, but that proves the route IS registered.
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        if response.status_code == 404:
            # Distinguish route-not-found from endpoint-not-found
            body = response.json()
            assert body.get("detail") != "Not Found", (
                "Schedule endpoint should be registered but got generic 404"
            )
        else:
            assert response.status_code != 405, (
                f"Schedule endpoint should be registered but got {response.status_code}"
            )

    def test_entrypoint_passes_content_policy_to_schedule_router(
        self,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Entrypoint passes content_policy to build_schedule_router."""
        monkeypatch.setenv("SCHEDULE_TRIGGER_ENABLED", "true")

        mock_build = mocker.patch(
            "cloud_agents.workflow.schedule_trigger.build_schedule_router",
        )
        # Return a minimal router so include_router doesn't fail
        from fastapi import APIRouter

        mock_build.return_value = APIRouter()

        import importlib

        import cloud_agents.workflow.temporal_entrypoint as ep_mod

        importlib.reload(ep_mod)
        ep_mod.build_temporal_app()

        assert mock_build.called
        call_kwargs = mock_build.call_args[1]
        assert "content_policy" in call_kwargs


# ===========================================================================
# 5. Prometheus metrics
# ===========================================================================


class TestScheduleTriggerMetrics:
    """Tests for schedule trigger Prometheus metrics."""

    def test_counter_incremented_on_create(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Counter incremented on schedule create."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        from cloud_agents.workflow.temporal_metrics import ls_schedule_triggers_total

        before = ls_schedule_triggers_total.labels(
            workflow_name="nightly-report", status="created"
        )._value.get()

        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)

        after = ls_schedule_triggers_total.labels(
            workflow_name="nightly-report", status="created"
        )._value.get()
        assert after > before

    def test_counter_incremented_on_delete(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Counter incremented on schedule delete."""
        mock_handle = mocker.AsyncMock()
        mock_handle.delete = mocker.AsyncMock()

        mock_temporal = mocker.MagicMock()
        mock_temporal.get_schedule_handle = mocker.MagicMock(return_value=mock_handle)

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        from cloud_agents.workflow.temporal_metrics import ls_schedule_triggers_total

        before = ls_schedule_triggers_total.labels(
            workflow_name="unknown", status="deleted"
        )._value.get()

        app = _build_schedule_app(mock_temporal)
        client = TestClient(app, raise_server_exceptions=False)
        client.delete("/v1/schedules/sched-abc")

        after = ls_schedule_triggers_total.labels(
            workflow_name="unknown", status="deleted"
        )._value.get()
        assert after > before


# ===========================================================================
# 6. Authorization actions
# ===========================================================================


class TestScheduleAuthorizationActions:
    """Tests for schedule-specific WorkflowAction values."""

    def test_schedule_actions_exist(self) -> None:
        """Schedule-specific actions exist in WorkflowAction enum."""
        from cloud_agents.workflow.authorization import WorkflowAction

        assert hasattr(WorkflowAction, "SCHEDULE_CREATE")
        assert hasattr(WorkflowAction, "SCHEDULE_VIEW")
        assert hasattr(WorkflowAction, "SCHEDULE_DELETE")
        assert hasattr(WorkflowAction, "SCHEDULE_PAUSE")
        assert hasattr(WorkflowAction, "SCHEDULE_RESUME")

    def test_schedule_actions_are_strings(self) -> None:
        """Schedule actions have string values."""
        from cloud_agents.workflow.authorization import WorkflowAction

        assert WorkflowAction.SCHEDULE_CREATE.value == "schedule_create"
        assert WorkflowAction.SCHEDULE_VIEW.value == "schedule_view"
        assert WorkflowAction.SCHEDULE_DELETE.value == "schedule_delete"
        assert WorkflowAction.SCHEDULE_PAUSE.value == "schedule_pause"
        assert WorkflowAction.SCHEDULE_RESUME.value == "schedule_resume"


# ===========================================================================
# 7. Audit event types
# ===========================================================================


class TestScheduleAuditEventTypes:
    """Tests for schedule-specific audit event types."""

    def test_schedule_event_types_in_literal(self) -> None:
        """Schedule event types are valid AuditEventType values."""
        from cloud_agents.workflow.audit import AuditEvent

        # These should not raise ValidationError
        AuditEvent(
            event_type="schedule_created",
            workflow_id="test",
        )
        AuditEvent(
            event_type="schedule_deleted",
            workflow_id="test",
        )
        AuditEvent(
            event_type="schedule_triggered",
            workflow_id="test",
        )


# ===========================================================================
# 8. RBAC enforcement
# ===========================================================================


class TestScheduleRBACEnforcement:
    """Tests for RBAC authorizer enforcement on schedule endpoints."""

    def _make_deny_authorizer(self, mocker: MockerFixture) -> Any:
        """Create an authorizer that denies all actions."""
        from cloud_agents.workflow.authorization import AuthzDecision

        authz = mocker.AsyncMock()
        authz.authorize = mocker.AsyncMock(
            return_value=AuthzDecision(allowed=False, reason="denied by policy")
        )
        return authz

    def test_create_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        authz = self._make_deny_authorizer(mocker)
        app = _build_schedule_app(
            mock_temporal, definition_store=store, authorizer=authz
        )
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 403
        assert "denied by policy" in response.json()["detail"]

    def test_list_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /v1/schedules returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        authz = self._make_deny_authorizer(mocker)

        app = _build_schedule_app(mock_temporal, authorizer=authz)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/schedules")
        assert response.status_code == 403

    def test_get_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /v1/schedules/{id} returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        authz = self._make_deny_authorizer(mocker)

        app = _build_schedule_app(mock_temporal, authorizer=authz)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/schedules/sched-abc")
        assert response.status_code == 403

    def test_delete_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """DELETE /v1/schedules/{id} returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        authz = self._make_deny_authorizer(mocker)

        app = _build_schedule_app(mock_temporal, authorizer=authz)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/v1/schedules/sched-abc")
        assert response.status_code == 403

    def test_pause_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules/{id}/pause returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        authz = self._make_deny_authorizer(mocker)

        app = _build_schedule_app(mock_temporal, authorizer=authz)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules/sched-abc/pause")
        assert response.status_code == 403

    def test_resume_denied_by_authorizer(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /v1/schedules/{id}/resume returns 403 when authorizer denies."""
        mock_temporal = mocker.MagicMock()
        authz = self._make_deny_authorizer(mocker)

        app = _build_schedule_app(mock_temporal, authorizer=authz)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules/sched-abc/resume")
        assert response.status_code == 403


# ===========================================================================
# 9. Content policy enforcement
# ===========================================================================


class TestScheduleContentPolicy:
    """Tests for content policy validation on schedule creation."""

    def test_create_rejected_by_content_policy(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST returns 422 when content policy rejects the definition."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        # Mock validate_definition to return errors
        mocker.patch(
            "cloud_agents.workflow.temporal_validation.validate_definition",
            return_value=["content policy: prompt too long"],
        )

        from cloud_agents.workflow.content_policy import ContentPolicy

        policy = ContentPolicy(max_prompt_length=5)

        app = _build_schedule_app(
            mock_temporal, definition_store=store, content_policy=policy
        )
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 422
        body = response.json()
        assert "validation_errors" in body.get("detail", {})

    def test_create_passes_without_content_policy(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST succeeds when no content policy is configured."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.create_schedule = mocker.AsyncMock()

        store = mocker.AsyncMock()
        store.get = mocker.AsyncMock(return_value=_make_stored_definition())

        mocker.patch("cloud_agents.workflow.schedule_trigger.emit_audit")

        # No content_policy passed --- should skip validation
        app = _build_schedule_app(mock_temporal, definition_store=store)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/schedules", json=SAMPLE_SCHEDULE_INPUT)
        assert response.status_code == 201
