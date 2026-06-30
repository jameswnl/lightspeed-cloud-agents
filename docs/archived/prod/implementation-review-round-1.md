# Review: Productization P0 implementation (round 1)

## Findings

### 1. High: MCP injection is still unreachable from the real workflow path
`WorkflowInput` and `run_sandbox_step()` both support `mcp_servers`, but the public `/run` API never accepts it and `AgentWorkflow._handle_agent_step()` never forwards `input.mcp_servers` into the activity payload. That means the new MCP serialization/mount logic only works in direct activity tests, not in actual workflow executions.

Recommended fix: add `mcp_servers` to `RunWorkflowRequest`, persist it into `WorkflowInput`, forward it from `AgentWorkflow` to `run_sandbox_step()`, and add at least one API-to-workflow integration test that asserts `LIGHTSPEED_MCP_SERVERS` reaches the spawned sandbox.

### 2. High: The documented 409/idempotency contract is not actually exposed to callers
The code now catches Temporal `ALREADY_EXISTS`, but `/v1/workflows/run` always generates a fresh random `workflow_id` server-side. Because callers cannot submit their own `workflow_id`, they cannot use it as an idempotency key, so duplicate client retries will still create duplicate workflows instead of returning 409 as the plan/docs claim.

Recommended fix: accept a caller-supplied `workflow_id` on the run request, validate it, pass it through unchanged to Temporal, and keep the 409 mapping on duplicate submissions.

### 3. Medium: Podman crash recovery will miss real orphaned sandboxes
Startup reconciliation filters with `{"spawned-by": "workflow-runner"}`, but the activity only sends `cloud-agents/*` labels and `PodmanSpawner` passes those labels through unchanged. Unlike `KubernetesSpawner`, the Podman path never injects `spawned-by=workflow-runner`, so `list_active()` will not find the containers it is supposed to clean up after a crash.

Recommended fix: make the Podman spawn path add the same durable runner label as Kubernetes, then add a Podman-specific reconciliation test that exercises the real label selector.

### 4. Medium: Secret-backed MCP headers on Podman degrade into a late runtime failure
The implementation plan said Podman should reject `secret_headers` with a clear error. Instead, the activity happily emits file-reference headers in `LIGHTSPEED_MCP_SERVERS`, while `PodmanSpawner` only logs a warning and ignores `mcp_secret_mounts`. The sandbox then receives file paths that were never mounted, so the failure moves downstream and becomes harder to diagnose.

Recommended fix: fail fast on the Podman path when secret-backed MCP headers are requested, and add a unit test that asserts the explicit error.

### 5. Medium: Structured audit coverage is only partially implemented
The new audit helper exists and activities emit `sandbox_spawned`, `sandbox_destroyed`, and `escalation_triggered`, but the reviewed batch does not emit the workflow-level events called out in the plan (`workflow_started`, approval decisions, startup `orphan_cleanup`, `mcp_secret_mounted`). The current tests also only prove the helper and activity-level events, not the full audit trail contract claimed by the docs.

Recommended fix: emit audit events from `AgentWorkflow` and startup reconciliation, add MCP mount auditing in the activity path, and add tests that assert those events from the caller-facing workflow flow rather than helper-only unit tests.

### 6. Medium: The image workflow still misses important runtime-changing paths
`.github/workflows/build_workflow_runner.yaml` only triggers on `deploy/workflow-runner/**`, `src/agents/workflow/**`, `pyproject.toml`, and `uv.lock`. This productization batch changes `src/agents/spawner/**` and `deploy/helm/cloud-agents-temporal/**`, both of which materially affect the shipped runtime/deployment path, but neither path currently triggers the image workflow.

Recommended fix: broaden the workflow path filters to include all files that can change the runner image or deployment behavior, especially `src/agents/spawner/**` and the Helm/kind manifests used by this feature.

## Perspective Check
- Functionality: significant gaps remain in MCP propagation, API idempotency, and Podman crash-recovery behavior.
- Quality: several tests are too low-level and miss the public seam; CI path coverage is also incomplete.
- Security: no new auth bypass stood out in the reviewed slice, but the audit trail and Podman secret-header handling are not yet production-ready.

## Verification
- `uv run pytest tests/unit/agents/workflow/temporal/test_activities.py -q` -> `30 passed`
- `uv run pytest tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_entrypoint.py tests/unit/agents/workflow/temporal/test_audit.py tests/unit/agents/workflow/temporal/test_structured_logging.py tests/unit/agents/workflow/temporal/test_startup_reconciliation.py -q` -> `41 passed, 1 warning`
- `uv run pytest tests/unit/agents/spawner/test_kubernetes_spawner.py -q` -> `17 passed`
- `uv run pytest tests/unit/agents/spawner/test_podman_spawner.py -q` printed `.....` and then hung; I terminated it manually, so Podman-path verification is incomplete
- `helm template smoke deploy/helm/cloud-agents-temporal` could not be run because `helm` is not installed in this environment

## Summary
Not LGTM. The current batch has real progress on credential mounts, MCP serialization, security context, and logging primitives, but there are still unresolved seam failures between the API, workflow engine, and spawner layer. The biggest blockers are that MCP config is not reachable through the real workflow path, the documented idempotency contract is not exposed to callers, and the Podman recovery/secret-header paths are not yet safe or coherent enough for the intended production scope.
