# Review: Phase 6 implementation (`0079f7f6..0c39afb6`)

## Findings

### 1. High: definition submissions are process-local and never drive execution
Phase 6's core contract is "submit/list/run by name from shared storage with immutable snapshots." The current implementation still runs a single startup-loaded definition, while the new definition API writes to an in-memory `DefinitionStore` that is local to one process. In a multi-replica deployment, a definition submitted to replica A will not exist on replica B, and `POST /v1/workflows/run` does not accept a `workflow_name` to select from that store.

Why it matters:
- the shared definition catalog is not actually shared across replicas
- runs are still bound to the startup YAML, not to a submitted definition
- `definition_version` and immutable snapshot binding are still missing from runtime state

Recommended fix:
- back `DefinitionStore` with shared persistence
- require `workflow_name` in `POST /v1/workflows/run`
- resolve and persist `definition_version` plus the definition snapshot when the run is created

### 2. High: the async callback/result-ingest path is still not implemented
The task doc says ephemeral agent steps must be dispatched asynchronously, marked `dispatched`, and completed via an authenticated callback endpoint with `step_id` and `attempt_id`. Instead, the new dispatcher explicitly says it still executes synchronously, and the executor still waits for the spawned pod's `client.run()` result inline.

Why it matters:
- the runner still blocks on ephemeral work instead of returning after dispatch
- there is no `/v1/workflows/steps/{step_id}/complete` contract yet
- there is no idempotent completion handling, stale-attempt rejection, or callback-triggered advancement path
- the intended trust boundary for untrusted ephemeral pods is not actually enforced in code

Recommended fix:
- wire `StepDispatcher` into `WorkflowExecutor`
- persist `dispatched` state plus `step_id` and `attempt_id`
- add the authenticated result-ingest endpoint
- make spawned agents report results via callback instead of being synchronously awaited by the runner

### 3. Medium: optimistic locking is not atomic in the persistence layer
The design calls for a DB compare-and-swap update. The current `save_with_version()` does a separate load and then calls a blind `save()`. `PostgresPersistence.save()` does not include a version predicate, so two replicas can both observe the same version and both write successfully.

Why it matters:
- the current code claims stale-write protection without actually enforcing it at the DB write boundary
- once callback-driven advancement exists, two replicas can still race to advance or fail the same workflow

Recommended fix:
- add a real CAS method to `WorkflowPersistence`
- implement it in PostgreSQL as a single `UPDATE ... WHERE workflow_id = ? AND version = ?`
- make advancement and recovery use that atomic path instead of load-then-save

### 4. Medium: stateless startup and multi-replica verification are still incomplete
The entrypoint still hard-requires a startup `workflow.yaml`, and the module only creates `app` when that file exists. That conflicts with the stated Phase 6 goal that the runner starts empty and receives definitions via API. The reviewed range also did not include deployment or E2E updates for the promised two-replica failover scenario.

Why it matters:
- the service is not yet bootstrappable as a stateless multi-definition runner
- the Phase 6 deployment claim is not reflected in committed assets
- the key failover behavior has not been verified beyond unit tests

Recommended fix:
- make startup independent of `workflow.yaml`
- serve the definition API even with an empty catalog
- add the manifest changes and two-replica E2E coverage described in `phase-6-tasks.md`

## Perspective Check
- Functionality: remaining gaps. Some scaffolding landed, but the central Phase 6 runtime contract is still incomplete.
- Quality: remaining gaps. Unit tests pass, but they mostly cover helper behavior and not the public multi-replica workflow contract.
- Security: remaining gaps. The intended callback-auth trust boundary for untrusted ephemeral pods is not implemented yet.

## Verification
- Reviewed Phase 6 scope from `docs/design/cloud-agents/phase-6-tasks.md`
- Reviewed implementation range `0079f7f6..0c39afb6`
- Read the changed workflow implementation files and matching tests together
- Ran `uv run pytest tests/unit/agents/workflow/test_definition_store.py tests/unit/agents/workflow/test_advancement.py -q` -> `15 passed`
- Ran `uv run pytest tests/unit/agents/workflow -q` -> `186 passed`
- Confirmed the reviewed implementation range did not include deployment or E2E changes for the promised stateless multi-replica rollout

## Summary
Not LGTM yet. The branch adds useful scaffolding for paused-step persistence, definition version objects, GraphExecutor deprecation, and a recovery poller, but it does not yet deliver the central Phase 6 promises: shared run-by-name definitions, immutable snapshot binding, authenticated result-ingest callbacks, and verified stateless multi-replica behavior.
