# Phase 7: Security Hardening + Operator Robustness Patterns

## Context

The Agentic Operator vs Cloud Agents comparison review (`kubeclaw-vs-cloudagents.md`) identified **3 critical + 2 high security issues** and **6 robustness patterns** from the operator that should be adopted. This phase addresses the 3 critical issues and 1 high issue (agent auth). One high-severity item (tool origin validation) is **intentionally deferred** — agent YAML is authored by platform teams, not end users, and tool loading is equivalent to Python's `import`. An optional allowlist is captured in the backlog.

The reviewer validated Cloud Agents as the right foundation but the critical security issues must be fixed before production deployment.

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
- When not set, **fail closed**: treat the step as `"high"` risk (requires manual approval). Log a warning: "No explicit risk_level — defaulting to high (manual approval required)"
- Keyword-based classification removed from the production approval path. Kept only as a debug utility function, never used for actual approval decisions
- Document: production workflows must set explicit risk_level on all agent steps

**Files:**
- Modify: `src/agents/workflow/definition.py` — add `risk_level` field
- Modify: `src/agents/workflow/auto_approve.py` — prefer explicit over keyword
- Update: example workflow YAMLs, tests

---

### Task 3: Auth on agent HTTP calls

**Problem:** `RemoteAgentClient.run()` sends HTTP requests to agent pods with no authentication. Any pod in the cluster can call any agent endpoint.

**Fix (common):**
- Add optional `auth_token` parameter to `RemoteAgentClient`
- When set, include `Authorization: Bearer {token}` header on all requests
- `BearerAuthMiddleware` already exists on the agent runtime — just need to wire the token through

**Fix (Podman / shared-secret mode):**
- Runner reads `AGENT_API_TOKEN` from host env
- `PodmanSpawner` passes `AGENT_API_TOKEN` as env var to spawned containers (host env propagation)
- `RemoteAgentClient` sends this token as Bearer header

**Fix (Kubernetes / SA token mode):**
- Spawned Jobs get a projected ServiceAccount token volume (`/var/run/secrets/tokens/agent-token`) with audience scoping
- Runner reads its own SA token from the projected volume path
- `RemoteAgentClient` sends the SA token as Bearer header
- Agent runtime validates incoming tokens via K8s TokenReview API (or trusts the SA token audience)
- `AGENT_API_TOKEN` env var is NOT used in K8s mode

**Files:**
- Modify: `src/agents/remote_agent_client.py` — add `auth_token` parameter, send Bearer header
- Modify: `src/agents/workflow/executor.py` — read token from env (Podman) or projected volume (K8s)
- Modify: `src/agents/spawner/kubernetes_spawner.py` — add projected token volume to Job spec
- Modify: `src/agents/spawner/podman_spawner.py` — pass `AGENT_API_TOKEN` env var
- Update: tests

**Token model (deployment-specific):**
- **Podman:** Shared bearer token from `AGENT_API_TOKEN` env var. Acceptable because Podman deployments have a single trust domain (host-level network).
- **Kubernetes (production):** Projected ServiceAccount tokens. Each spawned Job gets a short-lived, audience-scoped SA token via `projected` volume. The agent runtime validates the token against the K8s TokenReview API. This provides per-pod identity, not a shared secret.
- The spawner interface accepts an `auth_mode: Literal["shared_secret", "sa_token"]` config to select the model per deployment target.

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

### Task 5: Explicit cleanup via TTL + recovery poller (no owner references)

**Problem:** Spawned K8s Jobs are not tied to the workflow runner. If the runner crashes, orphaned Jobs persist.

**Design decision:** Do NOT use `ownerReferences`. Owner refs to the runner Pod would GC in-flight Jobs during normal rollouts. Owner refs to the Deployment don't provide crash-cleanup semantics. The stateless multi-replica model (Phase 6) means no single Pod owns a step.

**Fix:**
- **Completed Jobs:** `ttlSecondsAfterFinished: 300` — K8s auto-cleans after 5 minutes
- **Orphaned running Jobs:** The recovery poller detects dispatched steps past timeout, marks them failed in workflow state, then calls `spawner.destroy(spawned_name)` to delete the backing K8s Job + Service. This is the same `destroy()` path used in normal step cleanup.
- Add `spawner_labels` to spawned Jobs: `workflow-id`, `step-name`, `created-at` for visibility
- Manual fallback: `kubectl delete jobs -l spawned-by=workflow-runner` for emergency cleanup

**Cleanup responsibility chain:**
1. Normal path: executor `finally` block calls `spawner.destroy()` after step completes
2. Runner crash: recovery poller on another replica detects orphaned step → calls `spawner.destroy()` to kill the Job
3. TTL: completed Jobs self-clean after 300s regardless

**Files:**
- Modify: `src/agents/spawner/kubernetes_spawner.py` — add workflow labels to Jobs
- Modify: `src/agents/workflow/advancement.py` — recovery poller calls `spawner.destroy()` for orphaned steps
- Document: cleanup procedures in ARCHITECTURE.md

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
Task 2 (risk_level)   ──┤──→ Task 4 (content-hash) → Task 5 (TTL + poller cleanup)
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
- Runner crash → recovery poller detects orphaned step → calls spawner.destroy() → Job deleted
- Completed Jobs auto-clean after 300s (TTL)
- Load workflow from DB → status matches derive_status(steps)
