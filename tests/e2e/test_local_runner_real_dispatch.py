"""E2E test: LocalWorkflowRunner's real orchestration + real dispatch (issue #227).

Closes two coverage gaps found while surveying engine x spawn-mode x
spawner test coverage:

- Finding 2: `spawn: local` had no test that ran a real LLM call through
  `LocalWorkflowRunner`'s actual pydantic-graph state machine (approval
  gates, context threading, condition evaluation). Existing coverage was
  either graph-construction-only (test_workflow_yaml.py, no
  execution) or called SubprocessExecutor directly, bypassing
  graph_translator.py/LocalWorkflowRunner entirely
  (test_allowed_skills.py).
- Finding 3: test_local_executor.py mocker-patches
  get_step_executor itself, so it never exercises real dispatch to
  DirectExecutor/SubprocessExecutor through the actual factory.

This file drives LocalWorkflowRunner.start() end-to-end -- real
graph_translator dispatch (get_step_executor is NOT mocked), a real
auto-approved approval gate, real context interpolation between steps,
and a real LLM call for both spawn: none (DirectExecutor) and
spawn: local (SubprocessExecutor, a real forked child process).

Prerequisites:
  - OPENAI_API_KEY set in environment

Usage:
  OPENAI_API_KEY=sk-... uv run pytest tests/e2e/test_local_runner_real_dispatch.py -v
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import pytest

from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

TEST_MODEL = os.environ.get("TEST_LLM_MODEL", "gpt-4o-mini")

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="LocalWorkflowRunner real-dispatch e2e tests require OPENAI_API_KEY",
)


class _InMemoryRunStateStore:
    """Minimal, real (non-mocked) RunStateStore stand-in.

    Implements the subset of RunStateStore's contract that
    LocalWorkflowRunner calls, backed by a plain dict instead of
    PostgreSQL. Using a mock here (as test_local_executor.py
    does) would only prove the runner *called* the store, not that its
    state ended up self-consistent -- this lets the test assert on the
    real post-execution status/steps via get_status(), the same surface
    a caller of LocalWorkflowRunner actually uses.
    """

    def __init__(self) -> None:
        self._workflows: dict[str, dict[str, Any]] = {}

    async def create(
        self,
        workflow_id: str,
        workflow_name: str,
        definition: dict[str, Any],
        provider: dict[str, Any],
        authz_context: dict[str, Any],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_workflow_id: Optional[str] = None,
    ) -> None:
        if workflow_id in self._workflows:
            raise ValueError(f"Workflow '{workflow_id}' already exists")
        self._workflows[workflow_id] = {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "status": "running",
            "current_step": None,
            "steps": {},
            "events": [],
            "definition": definition,
            "provider": provider,
            "authz_context": authz_context,
            "workflow_context": {},
            "user_id": user_id,
            "session_id": session_id,
            "parent_workflow_id": parent_workflow_id,
        }

    async def get(self, workflow_id: str) -> Optional[dict[str, Any]]:
        state = self._workflows.get(workflow_id)
        return dict(state) if state is not None else None

    async def update_step(
        self,
        workflow_id: str,
        step_name: str,
        status: str,
        output: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        wf = self._workflows[workflow_id]
        wf["steps"][step_name] = {
            "step_name": step_name,
            "status": status,
            "output": output,
            "error": error,
        }
        wf["current_step"] = step_name

    async def append_event(self, workflow_id: str, event: dict[str, Any]) -> None:
        self._workflows[workflow_id]["events"].append(event)

    async def set_paused(self, workflow_id: str, step_name: str) -> None:
        wf = self._workflows[workflow_id]
        wf["status"] = "paused"
        wf["current_step"] = step_name

    async def resume(self, workflow_id: str) -> None:
        wf = self._workflows[workflow_id]
        wf["status"] = "running"
        wf["current_step"] = None

    async def mark_terminal(self, workflow_id: str, status: str) -> None:
        wf = self._workflows[workflow_id]
        wf["status"] = status
        wf["current_step"] = None

    async def update_workflow_context(self, workflow_id: str, context: dict[str, Any]) -> None:
        self._workflows[workflow_id]["workflow_context"] = context

    async def list_paused(self) -> list[str]:
        return [wid for wid, wf in self._workflows.items() if wf["status"] == "paused"]


async def _wait_for_terminal(runner: LocalWorkflowRunner, workflow_id: str, timeout: float = 120.0):
    """Poll get_status() until the workflow reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await runner.get_status(workflow_id)
        if status.is_terminal:
            return status
        await asyncio.sleep(0.5)
    raise AssertionError(f"Workflow '{workflow_id}' did not reach a terminal state in {timeout}s")


_SEVERITY_TOKEN = "critical-227"


def _workflow_definition() -> dict[str, Any]:
    """A 3-step workflow exercising spawn: none, an auto-approved gate, and spawn: local.

    - 'triage' (spawn: none / DirectExecutor) produces a structured
      output value.
    - 'approve' (human-approval, auto_approve=True) exercises the real
      approval-gate/auto-approve path.
    - 'fix' (spawn: local / SubprocessExecutor) receives 'triage's
      output via real context interpolation ({{ steps... }}) and is
      asked to echo it back verbatim -- if context threading or
      interpolation were broken, the echoed value would not match.
    """
    return {
        "apiVersion": "v1",
        "kind": "AgentWorkflow",
        "metadata": {"name": "local-runner-real-dispatch-e2e"},
        "spec": {
            "steps": [
                {
                    "name": "triage",
                    "type": "agent",
                    "spawn": "none",
                    "prompt": (
                        f'Respond with a JSON object with exactly one field '
                        f'"severity" whose value is exactly the string '
                        f'"{_SEVERITY_TOKEN}". Only output JSON, no other text.'
                    ),
                    "output_key": "diagnosis",
                    "output_schema": {
                        "type": "object",
                        "properties": {"severity": {"type": "string"}},
                        "required": ["severity"],
                    },
                    "timeout_seconds": 60,
                },
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                    "message": "Approve remediation?",
                },
                {
                    "name": "fix",
                    "type": "agent",
                    "spawn": "local",
                    "prompt": (
                        "The prior step reported severity: "
                        "{{ steps.diagnosis.output.severity }}. Respond with a "
                        'JSON object with exactly one field "echoed_severity" '
                        "whose value is exactly that severity string, "
                        "unmodified. Only output JSON, no other text."
                    ),
                    "output_key": "fix",
                    "output_schema": {
                        "type": "object",
                        "properties": {"echoed_severity": {"type": "string"}},
                        "required": ["echoed_severity"],
                    },
                    "timeout_seconds": 60,
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_local_runner_drives_real_dispatch_across_spawn_modes() -> None:
    """LocalWorkflowRunner.start() with real executor dispatch and a real LLM.

    get_step_executor is not mocked here -- the workflow is driven through
    the actual factory to DirectExecutor (spawn: none) and SubprocessExecutor
    (spawn: local), through the real pydantic-graph state machine, with a
    real auto-approve gate and real context interpolation between steps.
    """
    store = _InMemoryRunStateStore()
    runner = LocalWorkflowRunner(spawner=None, run_state_store=store)

    input_data = {
        "definition": _workflow_definition(),
        "provider": {
            "name": "openai",
            "model": TEST_MODEL,
            "credentials_secret": "OPENAI_API_KEY",
        },
        "approval_policy": {"auto_approve": True},
    }

    workflow_id = await runner.start(input_data)
    status = await _wait_for_terminal(runner, workflow_id)

    assert status.status == "completed", f"Workflow did not complete: {status.steps}"

    diagnosis = status.steps["diagnosis"]
    assert diagnosis["status"] == "completed", diagnosis
    assert diagnosis["output"]["severity"] == _SEVERITY_TOKEN

    approval = status.steps["approval"]
    assert approval["status"] == "completed", approval
    assert approval["output"]["auto_approved"] is True

    fix = status.steps["fix"]
    assert fix["status"] == "completed", fix
    assert fix["output"]["echoed_severity"] == _SEVERITY_TOKEN
