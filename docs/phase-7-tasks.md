# Phase 7: Security Hardening + Operator Robustness Patterns

## Context

The Agentic Operator vs Cloud Agents comparison review (`kubeclaw-vs-cloudagents.md`) identified **3 critical + 2 high security issues** and **6 robustness patterns** from the operator that should be adopted. This phase addresses all of them, grouped by priority.

The reviewer validated Cloud Agents as the right foundation but these issues must be fixed before production deployment.

---

## Priority 1 — Security (required before production)

### Task 1: K8s Secrets for API keys

**Problem:** `OPENAI_API_KEY` is passed as a plain env var in pod specs — visible in `kubectl describe`, logs, audit trails.

**Fix:**
- `KubernetesSpawner`: use `envFrom: secretKeyRef` instead of literal env vars for sensitive keys
- Add `secret_env_vars: dict[str, SecretKeyRef]` to spawner config
- `PodmanSpawner`: read from host env (already the pattern) — document that Podman deployers manage secrets via host-level mechanisms
- Never pass `OPENAI_API_KEY` as a literal in Job specs

**Files:**
- Modify: `src/agents/spawner/kubernetes_spawner.py` — secretKeyRef for sensitive env vars
- Modify: `src/agents/spawner/base.py` — add `SecretKeyRef` model
- Modify: `src/agents/workflow/executor.py` — remove literal API key from env dict
- Update: K8s manifests, tests

---

### Task 2: Explicit risk_level on WorkflowStepSpec

**Problem:** `_classify_step_risk()` uses keyword matching on step names/prompts. `"check-and-delete"` matches `"check"` first → LOW risk. `"remediation-check"` → HIGH risk (wrong).

**Fix:**
- Add `risk_level: Optional[Literal["low", "medium", "high", "critical"]]` to `WorkflowStepSpec`
- When `risk_level` is set explicitly, use it directly — skip keyword classification
- When not set, fall back to keyword classification with a logged warning: "No explicit risk_level — using keyword inference"
- Document: production workflows should always set explicit risk_level

**Files:**
- Modify: `src/agents/workflow/definition.py` — add `risk_level` field
- Modify: `src/agents/workflow/auto_approve.py` — prefer explicit over keyword
- Update: example workflow YAMLs, tests

---

### Task 3: Auth on agent HTTP calls

**Problem:** `RemoteAgentClient.run()` sends HTTP requests to agent pods with no authentication. Any pod in the cluster can call any agent endpoint.

**Fix:**
- Add optional `auth_token` parameter to `RemoteAgentClient`
- When set, include `Authorization: Bearer {token}` header on all requests
- `BearerAuthMiddleware` already exists on the agent runtime — just need to wire the token through
- Spawner passes `AGENT_API_TOKEN` env var to ephemeral pods (via Secret, not literal)
- Workflow runner reads token from env and passes to `RemoteAgentClient`

**Files:**
- Modify: `src/agents/remote_agent_client.py` — add `auth_token` parameter, send Bearer header
- Modify: `src/agents/workflow/executor.py` — pass token to client factory
- Modify: spawner — include `AGENT_API_TOKEN` in env (Secret ref for K8s)
- Update: tests

**Token type:** Shared bearer token read from `AGENT_API_TOKEN` env var on the workflow runner. The same token is passed to spawned pods and used by `RemoteAgentClient`. This is simpler than per-workflow or SA tokens. SA token injection (the operator pattern) is a future enhancement for K8s deployments. Shared secret is acceptable because the trust boundary is runner ↔ agent pods within the same cluster/network.

---

### Task 3b: Tool origin validation (out-of-scope note)

**Problem:** `load_tools()` uses `importlib.import_module()` with module paths from YAML. If YAML is user-supplied, this is a code injection vector.

**Decision:** Out of scope for Phase 7. Rationale:
- Agent YAML is authored by the platform team, not end users
- In production, YAML comes from ConfigMaps or OCI artifacts managed by ops, not from untrusted input
- The tool loading path is equivalent to Python's `import` — restricting it would break the framework's extensibility

**Future mitigation (backlog):** Add an optional `allowed_tool_modules` allowlist in the runner config. When set, `load_tools()` rejects modules not on the list.

---

## Priority 2 — Robustness (from operator patterns)

### Task 4: Content-hash naming for spawned pods

**Problem:** Spawned Jobs use `{agent}-{uuid}` names. Retries create duplicates. No idempotency.

**Fix:**
- Hash the step config (agent name, prompt hash, attempt number) into the Job/container name
- Same input = same name = safe retry (create-only idempotency)
- `KubernetesSpawner`: handle `AlreadyExists` on Job creation
- `PodmanSpawner`: check if container exists before creating

**Files:**
- Modify: `src/agents/spawner/kubernetes_spawner.py` — content-hash naming
- Modify: `src/agents/spawner/podman_spawner.py` — same
- Modify: `src/agents/workflow/executor.py` — pass step config to spawner for hashing

---

### Task 5: Owner references on spawned Jobs

**Problem:** Spawned K8s Jobs are not tied to the workflow runner. If the runner crashes, orphaned Jobs persist.

**Fix:**
- Set `ownerReferences` on spawned Jobs pointing to the workflow runner Deployment/Pod
- K8s garbage collection automatically cleans up Jobs when the owner is deleted
- Pass runner pod name via `HOSTNAME` env var (standard in K8s)

**Files:**
- Modify: `src/agents/spawner/kubernetes_spawner.py` — set ownerReferences on Jobs

---

### Task 6: Derive status from step results

**Problem:** `WorkflowState.status` is stored as a mutable field. It can drift from actual step results.

**Fix:**
- Add `derive_status(steps: dict[str, StepResult]) -> str` pure function to `state.py`
- Executor calls `derive_status()` instead of setting `state.status` directly
- On load from persistence, re-derive status to catch any drift

**Files:**
- Modify: `src/agents/workflow/state.py` — add `derive_status()` function
- Modify: `src/agents/workflow/executor.py` — use derive_status instead of direct assignment

---

### Task 7: Wire PermissionScope into generic_runner

**Problem:** `PermissionScope.effective_tools()` exists but is never called. Tool filtering from permissions doesn't actually happen.

**Fix:**
- When `request.context` contains `permissions` (allowed_tools/denied_tools), apply them in `create_generic_runner()`
- Reuse the advisory mode pattern: build a filtered agent variant

**Files:**
- Modify: `src/agents/runtime/generic_runner.py` — apply PermissionScope filtering
- Modify: `src/agents/workflow/executor.py` — pass step permissions in context

---

### Task 8: FilePersistence CAS

**Problem:** `FilePersistence` ignores version. Concurrent writes silently overwrite.

**Fix:**
- Implement `save_cas()` using tempfile + rename + version check
- Read current file, check version, write to tempfile, atomic rename

**Files:**
- Modify: `src/agents/workflow/persistence.py` — add `save_cas()` to `FilePersistence`

---

## Priority 3 — Completeness

### Task 9: Populate definition_snapshot on workflow start

**Problem:** `WorkflowState.definition_snapshot` is only set for run-by-name workflows. The default executor path doesn't set it.

**Fix:**
- Set `definition_snapshot` in `WorkflowExecutor.run()` for all workflows

**Files:**
- Modify: `src/agents/workflow/executor.py`

---

### Task 10: Integration tests

**Problem:** No tests for executor + spawner + client together.

**Fix:**
- Add integration tests with mocked spawner verifying the full dispatch → execute → cleanup lifecycle
- Add K8s/Podman spawner unit tests with mocked API clients

**Files:**
- Create: `tests/integration/agents/test_executor_spawner.py`
- Create: `tests/unit/agents/spawner/test_kubernetes_spawner.py`
- Create: `tests/unit/agents/spawner/test_podman_spawner.py`

---

## Task Dependencies

```
Task 1 (K8s Secrets)  ──┐
Task 2 (risk_level)   ──┤──→ Task 4 (content-hash) → Task 5 (owner refs)
Task 3 (agent auth)   ──┘
                              │
Task 6 (derive status)       │
Task 7 (PermissionScope)     │
Task 8 (FilePersistence CAS) │
                              │
                              v
                         Task 9 (definition snapshot)
                              │
                              v
                         Task 10 (integration tests)
```

Priority 1 (Tasks 1-3) first. Priority 2 (Tasks 4-8) can be parallel. Priority 3 (Tasks 9-10) last.

---

## Verification

```bash
uv run pytest tests/unit/agents/ examples/tests/ -q    # all tests pass
```

**Security verification:**
- `kubectl describe` a spawned Job → no plain API key in env vars
- Agent HTTP calls include Bearer token
- Explicit risk_level overrides keyword classification

**Robustness verification:**
- Retry a failed step → same Job name (content-hash), no duplicate
- Delete workflow runner → spawned Jobs cleaned up (owner refs)
- Load workflow from DB → status matches derive_status(steps)
