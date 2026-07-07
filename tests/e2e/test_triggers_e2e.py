"""E2E test: alert and schedule trigger integration with real Temporal server.

Validates:
- Alertmanager webhook POST triggers a workflow in Temporal
- Schedule CRUD (create, get, delete) works against real Temporal Schedules API
- Proper alert payload format with all required Alertmanager v4 fields
- Schedule uses nested ScheduleSpec structure

Prerequisites:
  - Running Temporal server (TEMPORAL_E2E_URL env var, default: localhost:7233)

Usage:
  TEMPORAL_E2E_URL=localhost:7233 uv run pytest tests/e2e/test_triggers_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

TEMPORAL_URL = os.environ.get("TEMPORAL_E2E_URL", "localhost:7233")


def _temporal_available() -> bool:
    """Check if Temporal server is reachable."""
    try:
        from temporalio.client import Client

        asyncio.run(Client.connect(TEMPORAL_URL))
        return True
    except Exception:
        return False


def _diagnostic_workflow_yaml() -> dict:
    """Return a minimal workflow definition for testing."""
    return {
        "apiVersion": "v1",
        "kind": "AgentWorkflow",
        "metadata": {"name": "test-trigger-workflow"},
        "spec": {
            "steps": [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Diagnose the issue described in the alert.",
                    "output_key": "diagnosis",
                    "timeout_seconds": 60,
                },
            ],
        },
    }


def _full_alertmanager_payload(
    alertname: str = "HighCPU",
    workflow_name: str = "test-trigger-workflow",
    fingerprint: str | None = None,
) -> dict:
    """Build a complete Alertmanager v4 webhook payload with all required fields.

    Parameters:
        alertname: Name of the alert.
        workflow_name: Target workflow via cloud_agents_workflow label.
        fingerprint: Alert fingerprint for dedup. Auto-generated if None.
    """
    fp = fingerprint or uuid.uuid4().hex[:16]
    return {
        "version": "4",
        "groupKey": f"{{alertname=\"{alertname}\"}}",
        "status": "firing",
        "receiver": "cloud-agents",
        "groupLabels": {"alertname": alertname},
        "commonLabels": {
            "alertname": alertname,
            "cloud_agents_workflow": workflow_name,
            "severity": "critical",
        },
        "commonAnnotations": {"summary": f"{alertname}: test alert"},
        "externalURL": "http://alertmanager.example.com",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "cloud_agents_workflow": workflow_name,
                    "severity": "critical",
                    "namespace": "prod",
                },
                "annotations": {
                    "summary": f"CPU > 90% on node-1",
                    "description": "High CPU usage detected in production.",
                },
                "startsAt": "2024-01-01T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.example.com/graph",
                "fingerprint": fp,
            },
        ],
    }


@pytest.mark.skipif(
    not _temporal_available(),
    reason=f"Temporal server not available at {TEMPORAL_URL} (set TEMPORAL_E2E_URL)",
)
@pytest.mark.asyncio
class TestAlertTriggerE2E:
    """POST Alertmanager webhook triggers a workflow in Temporal."""

    async def test_alert_triggers_workflow(self) -> None:
        """Full alert trigger flow: register definition, POST webhook, verify workflow starts."""
        from temporalio.client import Client
        from temporalio.worker import Worker

        from cloud_agents.workflow.alert_trigger import (
            AlertTriggerConfig,
            build_alert_router,
        )
        from cloud_agents.workflow.definition import WorkflowDefinition
        from cloud_agents.workflow.definition_store import DefinitionStore
        from cloud_agents.workflow.temporal_activities import (
            build_escalation_activity,
            run_sandbox_step,
            send_approval_notification,
        )
        from cloud_agents.workflow.temporal_workflow import AgentWorkflow

        client = await Client.connect(TEMPORAL_URL)
        queue = f"e2e-alert-{uuid.uuid4().hex[:8]}"

        # Register the workflow definition
        definition_store = DefinitionStore()
        defn = WorkflowDefinition.model_validate(_diagnostic_workflow_yaml())
        await definition_store.save(defn)

        # Build the alert router
        config = AlertTriggerConfig(
            workflow_name_label="cloud_agents_workflow",
            dedup_window_seconds=1,
        )

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        alert_router = build_alert_router(
            temporal_client=client,
            definition_store=definition_store,
            config=config,
        )
        app.include_router(alert_router)

        # Start a Temporal worker with stub activities
        all_activities = [run_sandbox_step, build_escalation_activity, send_approval_notification]
        async with Worker(
            client,
            task_queue="cloud-agents",
            workflows=[AgentWorkflow],
            activities=all_activities,
        ):
            # POST the alert webhook
            with TestClient(app) as test_client:
                payload = _full_alertmanager_payload()
                resp = test_client.post("/v1/webhooks/alertmanager", json=payload)

                assert resp.status_code == 200, f"Alert webhook returned {resp.status_code}: {resp.text}"
                data = resp.json()
                assert data["workflows_started"] == 1, f"Expected 1 workflow started, got {data}"
                assert data["errors"] == 0, f"Expected 0 errors, got {data}"

            # Give workflow a moment to execute
            await asyncio.sleep(2)

    async def test_alert_dedup_prevents_duplicate(self) -> None:
        """Duplicate alert within dedup window is skipped."""
        from temporalio.client import Client
        from temporalio.worker import Worker

        from cloud_agents.workflow.alert_trigger import (
            AlertTriggerConfig,
            build_alert_router,
        )
        from cloud_agents.workflow.definition import WorkflowDefinition
        from cloud_agents.workflow.definition_store import DefinitionStore
        from cloud_agents.workflow.temporal_activities import (
            build_escalation_activity,
            run_sandbox_step,
            send_approval_notification,
        )
        from cloud_agents.workflow.temporal_workflow import AgentWorkflow

        client = await Client.connect(TEMPORAL_URL)

        definition_store = DefinitionStore()
        defn = WorkflowDefinition.model_validate(_diagnostic_workflow_yaml())
        await definition_store.save(defn)

        config = AlertTriggerConfig(
            workflow_name_label="cloud_agents_workflow",
            dedup_window_seconds=300,
        )

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        alert_router = build_alert_router(
            temporal_client=client,
            definition_store=definition_store,
            config=config,
        )
        app.include_router(alert_router)

        all_activities = [run_sandbox_step, build_escalation_activity, send_approval_notification]
        async with Worker(
            client,
            task_queue="cloud-agents",
            workflows=[AgentWorkflow],
            activities=all_activities,
        ):
            with TestClient(app) as test_client:
                # Same fingerprint both times
                fingerprint = uuid.uuid4().hex[:16]
                payload = _full_alertmanager_payload(fingerprint=fingerprint)

                resp1 = test_client.post("/v1/webhooks/alertmanager", json=payload)
                assert resp1.json()["workflows_started"] == 1

                resp2 = test_client.post("/v1/webhooks/alertmanager", json=payload)
                assert resp2.json()["workflows_started"] == 0
                assert resp2.json()["alerts_skipped"] == 1

    async def test_alert_missing_workflow_definition_errors(self) -> None:
        """Alert referencing non-existent workflow definition produces an error."""
        from temporalio.client import Client

        from cloud_agents.workflow.alert_trigger import (
            AlertTriggerConfig,
            build_alert_router,
        )
        from cloud_agents.workflow.definition_store import DefinitionStore

        client = await Client.connect(TEMPORAL_URL)

        definition_store = DefinitionStore()  # Empty store
        config = AlertTriggerConfig(workflow_name_label="cloud_agents_workflow")

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        alert_router = build_alert_router(
            temporal_client=client,
            definition_store=definition_store,
            config=config,
        )
        app.include_router(alert_router)

        with TestClient(app) as test_client:
            payload = _full_alertmanager_payload(workflow_name="nonexistent-workflow")
            resp = test_client.post("/v1/webhooks/alertmanager", json=payload)

            data = resp.json()
            assert data["errors"] == 1
            assert data["workflows_started"] == 0


@pytest.mark.skipif(
    not _temporal_available(),
    reason=f"Temporal server not available at {TEMPORAL_URL} (set TEMPORAL_E2E_URL)",
)
@pytest.mark.asyncio
class TestScheduleTriggerE2E:
    """CRUD schedule operations against real Temporal Schedules API."""

    async def test_schedule_lifecycle(self) -> None:
        """Create, get, and delete a schedule against real Temporal."""
        from temporalio.client import Client

        from cloud_agents.workflow.definition import WorkflowDefinition
        from cloud_agents.workflow.definition_store import DefinitionStore
        from cloud_agents.workflow.schedule_trigger import build_schedule_router

        client = await Client.connect(TEMPORAL_URL)

        definition_store = DefinitionStore()
        defn_yaml = _diagnostic_workflow_yaml()
        # Add a default provider so schedules can use it
        defn_yaml["provider"] = {
            "name": "openai",
            "model": "gpt-4o",
            "credentials_secret": "OPENAI_API_KEY",
        }
        defn = WorkflowDefinition.model_validate(defn_yaml)
        await definition_store.save(defn)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        schedule_router = build_schedule_router(
            temporal_client=client,
            definition_store=definition_store,
        )
        app.include_router(schedule_router)

        schedule_id = f"e2e-sched-{uuid.uuid4().hex[:8]}"

        with TestClient(app) as test_client:
            # 1. Create a schedule with nested ScheduleSpec structure
            create_resp = test_client.post(
                "/v1/schedules",
                json={
                    "schedule_id": schedule_id,
                    "workflow_name": "test-trigger-workflow",
                    "schedule": {
                        "cron": "*/5 * * * *",
                        "timezone": "UTC",
                    },
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4o",
                        "credentials_secret": "OPENAI_API_KEY",
                    },
                    "sandbox_image": "lightspeed-agentic-sandbox:latest",
                },
            )
            assert create_resp.status_code == 201, (
                f"Expected 201, got {create_resp.status_code}: {create_resp.text}"
            )
            assert create_resp.json()["schedule_id"] == schedule_id

            # 2. Get the schedule
            get_resp = test_client.get(f"/v1/schedules/{schedule_id}")
            assert get_resp.status_code == 200, (
                f"Expected 200, got {get_resp.status_code}: {get_resp.text}"
            )
            schedule_info = get_resp.json()
            assert schedule_info["cron"] == "*/5 * * * *"
            assert schedule_info["workflow_name"] == "test-trigger-workflow"

            # 3. Delete the schedule
            delete_resp = test_client.delete(f"/v1/schedules/{schedule_id}")
            assert delete_resp.status_code == 200, (
                f"Expected 200, got {delete_resp.status_code}: {delete_resp.text}"
            )

            # 4. Verify it's gone
            gone_resp = test_client.get(f"/v1/schedules/{schedule_id}")
            assert gone_resp.status_code == 404

    async def test_schedule_duplicate_id_rejected(self) -> None:
        """Creating a schedule with duplicate ID returns 409."""
        from temporalio.client import Client

        from cloud_agents.workflow.definition import WorkflowDefinition
        from cloud_agents.workflow.definition_store import DefinitionStore
        from cloud_agents.workflow.schedule_trigger import build_schedule_router

        client = await Client.connect(TEMPORAL_URL)

        definition_store = DefinitionStore()
        defn = WorkflowDefinition.model_validate(_diagnostic_workflow_yaml())
        await definition_store.save(defn)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        schedule_router = build_schedule_router(
            temporal_client=client,
            definition_store=definition_store,
        )
        app.include_router(schedule_router)

        schedule_id = f"e2e-dup-{uuid.uuid4().hex[:8]}"
        body = {
            "schedule_id": schedule_id,
            "workflow_name": "test-trigger-workflow",
            "schedule": {"cron": "0 * * * *"},
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "OPENAI_API_KEY",
            },
        }

        with TestClient(app) as test_client:
            try:
                resp1 = test_client.post("/v1/schedules", json=body)
                assert resp1.status_code == 201

                resp2 = test_client.post("/v1/schedules", json=body)
                assert resp2.status_code == 409
            finally:
                # Cleanup even if assertions fail
                test_client.delete(f"/v1/schedules/{schedule_id}")

    async def test_schedule_nonexistent_workflow_rejected(self) -> None:
        """Creating a schedule for a nonexistent workflow returns 404."""
        from temporalio.client import Client

        from cloud_agents.workflow.definition_store import DefinitionStore
        from cloud_agents.workflow.schedule_trigger import build_schedule_router

        client = await Client.connect(TEMPORAL_URL)

        definition_store = DefinitionStore()  # Empty

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        schedule_router = build_schedule_router(
            temporal_client=client,
            definition_store=definition_store,
        )
        app.include_router(schedule_router)

        with TestClient(app) as test_client:
            resp = test_client.post(
                "/v1/schedules",
                json={
                    "workflow_name": "nonexistent-workflow",
                    "schedule": {"cron": "0 * * * *"},
                    "provider": {
                        "name": "openai",
                        "model": "gpt-4o",
                        "credentials_secret": "OPENAI_API_KEY",
                    },
                },
            )
            assert resp.status_code == 404
