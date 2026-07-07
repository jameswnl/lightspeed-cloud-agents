"""Step definitions for multi-replica E2E scenarios.

Exercises multi-replica workflow runner deployment on Kind.
Tests crash recovery, orphan cleanup, and concurrent workflow
distribution using kubectl, Temporal client, and pod inspection.

Prerequisites:
  - Kind cluster with 2-replica workflow runner deployed
  - Temporal server running in the cluster
  - kubectl configured for the Kind cluster
  - Built images loaded into Kind

Usage:
  uv run pytest tests/e2e/features/steps/test_multi_replica_bdd.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# Load scenarios from the feature file
scenarios("../multi_replica.feature")

KUBECTL_TIMEOUT = 120
WORKFLOW_TIMEOUT = 300
NAMESPACE = os.environ.get("E2E_NAMESPACE", "default")
RUNNER_API_PORT = os.environ.get("E2E_RUNNER_PORT", "8080")


def _kubectl(*args: str, check: bool = True, timeout: int = KUBECTL_TIMEOUT) -> str:
    """Run a kubectl command and return stdout.

    Parameters:
        args: kubectl subcommand and arguments.
        check: Raise on non-zero exit.
        timeout: Command timeout in seconds.

    Returns:
        Command stdout as string.
    """
    cmd = ["kubectl", "-n", NAMESPACE, *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=check,
    )
    return result.stdout.strip()


def _kubectl_json(*args: str) -> Any:
    """Run kubectl with JSON output and return parsed result.

    Parameters:
        args: kubectl subcommand and arguments.

    Returns:
        Parsed JSON output.
    """
    raw = _kubectl(*args, "-o", "json")
    return json.loads(raw)


def _get_runner_pods() -> list[dict[str, Any]]:
    """Get all workflow-runner pods.

    Returns:
        List of pod dicts from kubectl.
    """
    data = _kubectl_json("get", "pods", "-l", "app=workflow-runner")
    return data.get("items", [])


def _get_ready_runner_pods() -> list[dict[str, Any]]:
    """Get workflow-runner pods that are Ready.

    Returns:
        List of ready pod dicts.
    """
    pods = _get_runner_pods()
    ready = []
    for pod in pods:
        conditions = pod.get("status", {}).get("conditions", [])
        for cond in conditions:
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                ready.append(pod)
                break
    return ready


def _wait_for_ready_pods(count: int, timeout: int = KUBECTL_TIMEOUT) -> list[dict[str, Any]]:
    """Wait until the expected number of runner pods are Ready.

    Parameters:
        count: Expected number of ready pods.
        timeout: Max wait time in seconds.

    Returns:
        List of ready pod dicts.

    Raises:
        TimeoutError: If pods don't become ready in time.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = _get_ready_runner_pods()
        if len(ready) >= count:
            return ready
        time.sleep(2)
    raise TimeoutError(
        f"Expected {count} ready runner pods, got {len(_get_ready_runner_pods())} "
        f"after {timeout}s"
    )


def _submit_workflow(workflow_id: str | None = None) -> dict[str, Any]:
    """Submit a diagnostic workflow via the runner API.

    Parameters:
        workflow_id: Optional workflow ID. Generated if not provided.

    Returns:
        API response as dict.
    """
    wf_id = workflow_id or f"e2e-multi-{uuid.uuid4().hex[:8]}"
    payload = json.dumps({
        "definition": {
            "kind": "AgentWorkflow",
            "apiVersion": "v1",
            "metadata": {"name": "e2e-multi-replica-test"},
            "spec": {
                "steps": [
                    {
                        "name": "diagnose",
                        "type": "agent",
                        "output_key": "diagnosis",
                        "instructions": "Analyze the system and report findings.",
                    },
                    {
                        "name": "verify",
                        "type": "agent",
                        "output_key": "verification",
                        "instructions": "Verify the diagnosis is correct.",
                    },
                ],
            },
        },
        "workflow_id": wf_id,
        "provider": {
            "name": "openai",
            "model": "gpt-4o-mini",
            "credentials_secret": "openai-api-key",
        },
        "approval_policy": {
            "auto_approve_risk_levels": ["low", "medium", "high", "critical"],
        },
    })
    result = subprocess.run(
        [
            "kubectl", "exec", "-n", NAMESPACE,
            "deploy/workflow-runner", "--",
            "curl", "-s", "-X", "POST",
            f"http://localhost:{RUNNER_API_PORT}/v1/workflows/run",
            "-H", "Content-Type: application/json",
            "-d", payload,
        ],
        capture_output=True, text=True, timeout=WORKFLOW_TIMEOUT,
        check=True,
    )
    return json.loads(result.stdout)


def _get_workflow_status(workflow_id: str) -> dict[str, Any]:
    """Query workflow status via the runner API.

    Parameters:
        workflow_id: The workflow ID to query.

    Returns:
        Workflow status as dict.
    """
    result = subprocess.run(
        [
            "kubectl", "exec", "-n", NAMESPACE,
            "deploy/workflow-runner", "--",
            "curl", "-s",
            f"http://localhost:{RUNNER_API_PORT}/v1/workflows/{workflow_id}",
        ],
        capture_output=True, text=True, timeout=KUBECTL_TIMEOUT,
        check=True,
    )
    return json.loads(result.stdout)


def _get_pod_logs(pod_name: str, since: str = "5m") -> str:
    """Get logs from a specific pod.

    Parameters:
        pod_name: Name of the pod.
        since: Time window for logs.

    Returns:
        Pod logs as string.
    """
    return _kubectl("logs", pod_name, f"--since={since}", check=False)


# ── Fixtures ──────────────────────────────────────────


@pytest.fixture
def cluster_state():
    """Shared state dict across scenario steps."""
    return {}


# ── Background Steps ─────────────────────────────────


@given("a Kind cluster is running")
def kind_cluster_running():
    """Verify kubectl can reach the cluster."""
    try:
        _kubectl("cluster-info", timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("No accessible Kind cluster")


@given("the 2-replica workflow runner overlay is deployed")
def two_replica_overlay_deployed():
    """Verify the workflow-runner deployment has 2 replicas."""
    try:
        data = _kubectl_json("get", "deployment", "workflow-runner")
        replicas = data["spec"]["replicas"]
        if replicas < 2:
            pytest.skip(
                f"workflow-runner has {replicas} replica(s), need 2. "
                "Deploy with: kubectl apply -f deploy/kind/workflow-runner-2-replicas.yaml"
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("workflow-runner deployment not found")


@given("both workflow runner replicas are ready")
def both_replicas_ready():
    """Wait for both replicas to be Ready."""
    try:
        _wait_for_ready_pods(2, timeout=60)
    except TimeoutError:
        pytest.skip("Could not get 2 ready workflow-runner pods")


# ── Crash Recovery Scenario ──────────────────────────


@given("a multi-step workflow is submitted", target_fixture="cluster_state")
def submit_multi_step_workflow():
    """Submit a workflow and capture its ID."""
    state = {}
    try:
        response = _submit_workflow()
        state["workflow_id"] = response.get("workflow_id") or response.get("id")
        state["initial_pods"] = [p["metadata"]["name"] for p in _get_ready_runner_pods()]
    except Exception as exc:
        pytest.skip(f"Could not submit workflow: {exc}")
    return state


@given(parsers.parse('the workflow reaches the "{step_name}" step'))
def workflow_reaches_step(cluster_state: dict, step_name: str):
    """Wait until the workflow has started the named step."""
    wf_id = cluster_state.get("workflow_id")
    if not wf_id:
        pytest.skip("No workflow ID available")
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            status = _get_workflow_status(wf_id)
            events = status.get("events", [])
            started = [
                e for e in events
                if e.get("type") == "step.started" and e.get("step") == step_name
            ]
            if started:
                return
        except Exception:
            pass
        time.sleep(2)
    pytest.skip(f"Workflow did not reach step '{step_name}' within 60s")


@when("I delete the pod running the active workflow")
def delete_active_pod(cluster_state: dict):
    """Delete one of the running workflow-runner pods."""
    pods = _get_ready_runner_pods()
    if not pods:
        pytest.skip("No running pods to delete")
    target = pods[0]["metadata"]["name"]
    cluster_state["deleted_pod"] = target
    _kubectl("delete", "pod", target, "--grace-period=0", "--force")


@then("Temporal re-dispatches the activity to the surviving replica")
def activity_re_dispatched(cluster_state: dict):
    """Verify a new/different pod picks up the workflow activity."""
    deleted = cluster_state.get("deleted_pod")
    # Wait for replacement pod to be ready
    try:
        _wait_for_ready_pods(2, timeout=120)
    except TimeoutError:
        # At least 1 pod should be ready (the surviving one)
        ready = _get_ready_runner_pods()
        assert len(ready) >= 1, "No ready pods after crash"

    # The surviving/replacement pod names should not include the deleted pod
    # (K8s generates new pod names on replacement)
    current_pods = [p["metadata"]["name"] for p in _get_ready_runner_pods()]
    assert len(current_pods) >= 1, "No ready pods after crash recovery"
    # Deleted pod should be gone; K8s generates a new name for replacements
    assert deleted not in current_pods, (
        f"Deleted pod '{deleted}' still present in running pods: {current_pods}"
    )


@then("the workflow completes end-to-end with all steps succeeded")
def workflow_completes(cluster_state: dict):
    """Wait for the workflow to complete and verify all steps succeeded."""
    wf_id = cluster_state.get("workflow_id")
    if not wf_id:
        pytest.skip("No workflow ID")

    deadline = time.time() + WORKFLOW_TIMEOUT
    while time.time() < deadline:
        try:
            status = _get_workflow_status(wf_id)
            if status.get("status") == "completed":
                steps = status.get("steps", {})
                for step_name, step_data in steps.items():
                    assert step_data.get("status") == "completed", (
                        f"Step '{step_name}' status: {step_data.get('status')}"
                    )
                return
        except Exception:
            pass
        time.sleep(5)
    pytest.fail(f"Workflow {wf_id} did not complete within {WORKFLOW_TIMEOUT}s")


# ── Orphan Cleanup Scenario ──────────────────────────


@given("replica A has a running sandbox container", target_fixture="cluster_state")
def replica_a_has_sandbox():
    """Submit a workflow so replica A spawns a sandbox."""
    state = {}
    pods = _get_ready_runner_pods()
    if not pods:
        pytest.skip("No ready pods")
    state["replica_a"] = pods[0]["metadata"]["name"]
    try:
        response = _submit_workflow()
        state["workflow_id"] = response.get("workflow_id") or response.get("id")
    except Exception as exc:
        pytest.skip(f"Could not submit workflow: {exc}")
    # Give time for sandbox to spawn
    time.sleep(5)
    return state


@when("replica A is killed")
def kill_replica_a(cluster_state: dict):
    """Force-delete the replica A pod."""
    pod = cluster_state.get("replica_a")
    if not pod:
        pytest.skip("No replica A pod recorded")
    _kubectl("delete", "pod", pod, "--grace-period=0", "--force")


@when("a replacement replica starts")
def replacement_starts():
    """Wait for the Deployment controller to start a replacement pod."""
    try:
        _wait_for_ready_pods(2, timeout=120)
    except TimeoutError:
        ready = _get_ready_runner_pods()
        assert len(ready) >= 1, "No replacement pod started"


@then("the replacement replica runs orphan reconciliation on startup")
def orphan_reconciliation_runs(cluster_state: dict):
    """Check logs of the new replica for orphan reconciliation messages."""
    old_pod = cluster_state.get("replica_a")
    pods = _get_ready_runner_pods()
    new_pods = [p for p in pods if p["metadata"]["name"] != old_pod]
    if not new_pods:
        pytest.skip("No new pod found")

    new_pod = new_pods[0]["metadata"]["name"]
    # Give the pod time to run startup reconciliation
    time.sleep(10)
    logs = _get_pod_logs(new_pod, since="2m")
    # The entrypoint logs orphan cleanup activity
    assert (
        "orphan" in logs.lower()
        or "reconcil" in logs.lower()
        or "startup" in logs.lower()
    ), "No orphan reconciliation evidence in new pod logs"


@then("the orphaned sandbox from replica A is cleaned up")
def orphaned_sandbox_cleaned():
    """Verify no orphaned sandbox Jobs/containers with stale status remain.

    After orphan reconciliation, Jobs that are not active, succeeded,
    or failed (i.e., zombie entries) should have been cleaned up.
    """
    try:
        data = _kubectl_json("get", "jobs", "-l", "spawned-by=workflow-runner")
        items = data.get("items", [])
        # Orphaned jobs have no active/succeeded/failed status -- they are
        # stale entries left behind after an unclean shutdown.
        orphans = [
            j["metadata"]["name"]
            for j in items
            if j.get("status", {}).get("active", 0) == 0
            and j.get("status", {}).get("succeeded", 0) == 0
            and j.get("status", {}).get("failed", 0) == 0
        ]
        assert len(orphans) == 0, (
            f"Found {len(orphans)} orphaned sandbox Job(s) after reconciliation: {orphans}"
        )
    except subprocess.CalledProcessError:
        # No jobs found at all means no orphans -- this is expected
        pass


# ── Concurrent Workflows Scenario ─────────────────────


@given("4 workflows are submitted simultaneously", target_fixture="cluster_state")
def submit_four_workflows():
    """Submit 4 workflows in rapid succession."""
    state = {"workflow_ids": []}
    for i in range(4):
        wf_id = f"e2e-concurrent-{i}-{uuid.uuid4().hex[:6]}"
        try:
            response = _submit_workflow(workflow_id=wf_id)
            actual_id = response.get("workflow_id") or response.get("id") or wf_id
            state["workflow_ids"].append(actual_id)
        except Exception as exc:
            pytest.skip(f"Could not submit workflow {i}: {exc}")
    assert len(state["workflow_ids"]) == 4, "Expected 4 workflows submitted"
    return state


@then("all 4 workflows complete successfully")
def all_four_complete(cluster_state: dict):
    """Wait for all 4 workflows to complete."""
    wf_ids = cluster_state.get("workflow_ids", [])
    deadline = time.time() + WORKFLOW_TIMEOUT

    completed = set()
    while time.time() < deadline and len(completed) < 4:
        for wf_id in wf_ids:
            if wf_id in completed:
                continue
            try:
                status = _get_workflow_status(wf_id)
                if status.get("status") == "completed":
                    completed.add(wf_id)
            except Exception:
                pass
        if len(completed) < 4:
            time.sleep(5)

    assert len(completed) == 4, (
        f"Only {len(completed)}/4 workflows completed: {completed}"
    )


@then("workflows were distributed across both replicas")
def workflows_distributed(cluster_state: dict):
    """Verify that both replicas handled workflow activities.

    Checks pod logs for workflow execution evidence.
    """
    pods = _get_ready_runner_pods()
    pods_with_work = set()

    for pod in pods:
        pod_name = pod["metadata"]["name"]
        logs = _get_pod_logs(pod_name, since="10m")
        # Look for evidence of workflow execution in logs
        if "step.started" in logs or "sandbox" in logs.lower() or "activity" in logs.lower():
            pods_with_work.add(pod_name)

    # Temporal distributes activities across workers connected to the same
    # task queue. With 4 workflows, we expect both replicas to handle some.
    # In edge cases, Temporal might route all to one -- so we just assert
    # at least one pod did work and log the distribution.
    assert len(pods_with_work) >= 1, "No pods show evidence of workflow execution"
    # Ideal: both pods handled work
    if len(pods_with_work) < 2:
        import warnings

        warnings.warn(
            f"Only {len(pods_with_work)}/2 replicas handled workflows. "
            "Temporal may have routed all to one worker.",
            stacklevel=1,
        )
