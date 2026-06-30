# Agentic Operator vs Cloud Agents Framework — Comparison & Review

**Date**: 2026-06-24
**Methodology**: Hands-on testing of both systems (operator deployed on Kind/Podman with real GPT-5.5 agent, Cloud Agents reviewed at source level), full source code reading of both repos.

## Context

The **Lightspeed Agentic Operator** ([openshift/lightspeed-agentic-operator](https://github.com/openshift/lightspeed-agentic-operator) + [openshift/lightspeed-agentic-sandbox](https://github.com/openshift/lightspeed-agentic-sandbox)) is the current OpenShift Lightspeed agent workflow system. It's a Kubernetes operator that orchestrates AI-driven remediation workflows with sandbox isolation, approval gates, and RBAC scoping.

The **Cloud Agents Framework** (`lightspeed-stack` cloud-agents branch, module: `src/agents/`) is a PoC for a more general-purpose agent/workflow platform that supports both OCP and Podman deployment targets, enabling adoption by product teams beyond Lightspeed.

This document compares the two approaches, assesses reusability, identifies gaps, and includes a deep review of the Cloud Agents implementation with concrete improvement recommendations.

> **Note**: Line numbers reference the Cloud Agents codebase as of the `cloud-agents` branch HEAD at time of writing. These may shift as the code evolves — use the function/class names for durable references.

## Architecture Comparison

| Concern | Agentic Operator | Cloud Agents Framework |
|---|---|---|
| **Orchestration** | Kubernetes controller (Go reconcile loop) | Python workflow executor (async loop) |
| **State storage** | Kubernetes CRDs in etcd | PostgreSQL / in-memory / file (pluggable) |
| **Step dispatch** | SandboxClaim → Sandbox pod (agent-sandbox CRDs) | Spawner → K8s Job or Podman container |
| **Agent runtime** | Provider-specific SDKs (Claude Code, OpenAI agents, Gemini ADK) | Pydantic AI (single SDK, any LLM provider) |
| **Agent config** | Hardcoded in operator Go code (prompt templates, JSON schemas) | Declarative YAML (`agent.yaml` + `tools.py`) |
| **Workflow config** | Implicit in Proposal CRD spec (fixed 4-step pipeline with optional steps) | Declarative YAML (`workflow.yaml` with conditions, retry, branching) |
| **Approval** | ApprovalPolicy CRD + ProposalApproval CRD | ApprovalPolicy class + workflow pause/resume |
| **Podman support** | None (requires Kubernetes API, CRDs, agent-sandbox) | First-class (`PodmanSpawner`) |
| **Structured output** | JSON Schema sent via HTTP, SDK-specific enforcement per provider | Pydantic models, framework-level validation |
| **Tools** | OCI image volumes + SDK-native tools (Bash/Read/Glob) | Python modules loaded dynamically from `tools.py` |
| **MCP servers** | CRD config exists, sandbox TODO | Implemented (auth resolution, toolset creation) |
| **Observability** | Operator logs, Kubernetes events | OpenTelemetry tracing, Prometheus metrics, SSE streaming |
| **Extensibility** | Fork the operator, change Go code | Drop in YAML + Python, no framework changes |
| **Scaling** | Single controller replica (leader election) | Stateless horizontal scaling behind LB |

## Reusable Patterns from the Operator

### 1. Proposal lifecycle phases
The operator's lifecycle is well-designed: `Pending → Analyzing → Proposed → Executing → Verifying → Completed/Failed/Escalated`. The Cloud Agents framework already implements equivalent phases via workflow steps.

### 2. RBAC scoping from agent output
The operator dynamically creates Kubernetes Roles and ClusterRoles based on the analysis step's declared RBAC requirements. The execution sandbox pod gets only the permissions the agent said it needs (`controller/proposal/rbac.go`). This is a strong security pattern worth adopting.

### 3. Approval policy model
Per-step auto/manual approval from a cluster-wide policy, with per-proposal overrides. Both approaches implement this.

### 4. Retry with escalation handoff
Failed steps retry with context from previous attempts. After exhausting retries, an escalation document is generated for human operators. Both approaches implement this.

### 5. Config-hashed template reuse
The operator computes a hash of the step configuration (agent, model, tools, secrets) and reuses derived SandboxTemplates when the hash matches. This avoids redundant template creation and enables garbage collection of stale templates.

### 6. Condition-based phase derivation
The operator never stores phase/status directly. Instead, `DerivePhase()` is a pure function that computes the current phase from Kubernetes conditions on every reconcile. This prevents status from drifting out of sync with actual step results.

### 7. Content-addressed idempotency
Resources are named with content hashes (e.g., `ls-analysis-default-455ccfefb6`). Same input = same name = safe retry. Combined with create-only idempotency (`Create` + handle `AlreadyExists`), this makes the reconciler crash-safe.

## What the Cloud Agents Framework Solves That the Operator Cannot

### 1. Podman deployment (the critical gap)
The operator is architecturally Kubernetes-native: it depends on CRDs, the Kubernetes API server, controller-runtime, and `kubernetes-sigs/agent-sandbox` CRDs. None of these exist in a Podman environment. This is not a feature gap that can be bridged with configuration — it's a fundamental architectural constraint.

The Cloud Agents framework's spawner abstraction (`AgentSpawner` ABC with `KubernetesSpawner` and `PodmanSpawner` implementations) enables the same workflow to run on both targets.

### 2. Bring-your-own-agents via YAML
The operator's prompt templates, output schemas, and step logic are hardcoded in Go source files:
- `controller/proposal/templates/analysis_query.tmpl`
- `controller/proposal/templates/execution_query.tmpl`
- `controller/proposal/schemas.go` (JSON schemas per step)

Adding a new agent type or modifying the analysis schema requires changing operator code, rebuilding the image, and redeploying. The Cloud Agents framework's YAML-driven approach (`agent.yaml` + `tools.py` + `workflow.yaml`) lets product teams define agents without touching framework code.

### 3. Single generic agent runtime
The operator's sandbox container ships three SDK runtimes (Claude Code via Node.js, OpenAI agents, Google ADK) and selects at startup via `LIGHTSPEED_AGENT_PROVIDER`. The Cloud Agents framework uses a single runtime image with Pydantic AI, which supports any LLM provider through a unified interface.

### 4. Flexible workflow definition
The operator has a fixed 4-step pipeline: Analysis → Execution → Verification → Escalation. The sequence is hardcoded in the CRD (`SandboxStep` enum with CEL validation) and the reconciler. Execution and Verification can be omitted (giving advisory-only, assisted, or full remediation patterns), but you cannot reorder steps, add custom steps between them, or run steps in parallel. The Cloud Agents framework supports arbitrary step counts, conditional branching, template-based prompt interpolation between steps, and parallel step groups.

### 5. PostgreSQL-backed state
The operator stores all state in Kubernetes CRDs (etcd). This means:
- State is tied to a Kubernetes cluster
- No horizontal scaling of the controller (single leader)
- Recovery depends on Kubernetes watch/cache semantics

The Cloud Agents framework uses PostgreSQL with optimistic locking, enabling stateless horizontal scaling and crash recovery across replicas.

### 6. Observability
The operator has basic Go logging. The Cloud Agents framework has OpenTelemetry distributed tracing, per-tool Prometheus metrics, and SSE event streaming for real-time workflow progress.

## Gaps in the Cloud Agents Framework

### 1. No CRD/operator mode for OCP-native experience
OCP customers expect Kubernetes-native workflow management: `kubectl apply`, GitOps integration, RBAC via ServiceAccounts, status via `kubectl get`. A thin CRD-to-executor bridge operator would provide this without duplicating the workflow logic.

### 2. Dynamic RBAC from agent output
The operator creates per-step Roles/ClusterRoles based on what the analysis agent declares it needs. The Cloud Agents framework has static `permissions.service_account` per workflow step but doesn't dynamically scope RBAC from agent output.

### 3. Skills as OCI image volumes
The operator mounts skills from OCI container images as Kubernetes image volumes. This is more portable than ConfigMap/volume mounts and works across both K8s and Podman (Podman supports OCI image volumes natively). The Cloud Agents framework currently uses ConfigMap mounts for tools and skills.

### 4. Production hardening for Kubernetes
The operator has:
- CEL validation rules on CRDs (immutable specs, field-level constraints)
- Owner references for cascading garbage collection
- Finalizers for cleanup of child resources (RBAC, sandboxes)
- Controller-runtime watches for cache-based reconciliation

The Cloud Agents framework would need equivalent guardrails when running in Kubernetes mode.

### 5. Console UI
The operator has a dedicated OpenShift console plugin (`lightspeed-agentic-console`). The Cloud Agents framework exposes SSE events but has no UI.

---

## Cloud Agents Framework — Deep Review

### Security Findings

#### Critical

1. **API key exposure** (`WorkflowExecutor._execute_agent_step()` in `executor.py`)
   - `OPENAI_API_KEY` passed as plain env var in pod specs, visible in `kubectl describe`, logs, audit trails
   - **Operator pattern**: Uses K8s Secrets + `envFrom` secretRef + volume mounts
   - **Fix**: Inject credentials via K8s Secret reference, not literal env vars

2. **Keyword-based risk classification** (`_classify_step_risk()` in `auto_approve.py`)
   - Substring matching is unsafe: step named `"check-and-delete"` matches `"check"` first → classified as LOW risk
   - Step named `"remediation-check"` → classified as HIGH risk (wrong)
   - **Operator pattern**: Risk is explicit in workflow spec, not inferred from step names
   - **Fix**: Add explicit `risk_level` field to `WorkflowStepSpec`; remove keyword fallback or make it debug-only

3. **Unauthenticated agent endpoints** (`RemoteAgentClient.run()` in `remote_agent_client.py`)
   - HTTP calls to agent pods carry no bearer token, mTLS, or API key
   - Any pod in the cluster can call any agent endpoint
   - **Operator pattern**: Uses Kubernetes service account tokens for pod identity
   - **Fix**: Add configurable bearer token or service account token injection

#### High

4. **Advisory mode doesn't enforce** (`AdvisoryEnforcer` in `advisory.py`)
   - Tool filtering only works for registered tools. If the agent runs `kubectl delete` via shell, advisory mode can't intercept
   - **Operator pattern**: Creates read-only RBAC role at the container level
   - **Fix**: Document as "prompt annotation, not a security boundary"; for K8s, enforce via RBAC

5. **No tool origin validation** (`load_tools()` in `tool_loader.py`)
   - Any Python module path can be loaded via `importlib.import_module()`
   - If tool module path is user-controlled (from YAML), code injection is possible
   - **Fix**: Restrict to an allowlist of module paths or validate against a registry

#### Medium

6. **No TLS validation on MCP servers** (`load_mcp_servers()` in `mcp_loader.py`)
   - `MCPToolset` created with default httpx settings, no certificate override
   - **Fix**: Add optional `tls_verify` field to `MCPServerSpec`

7. **Condition expression parsing** (`evaluate_condition()` in `conditions.py`)
   - String comparison has no escaping support — `"it's"` would parse incorrectly
   - **Fix**: Use `shlex` or JSON parsing for string literal values

### Operator Patterns Worth Adopting

| Operator Pattern | Current Gap in Cloud Agents | Where to Apply |
|---|---|---|
| **Content-hash resource naming** | K8s Jobs created with deterministic but non-hashed names; retries create duplicates | `KubernetesSpawner.spawn()` — hash agent config into Job name |
| **Owner references for GC** | Spawned Jobs/containers not tied to parent; orphaned on runner crash | `KubernetesSpawner` — set `ownerReferences` on Jobs |
| **Condition-based phase derivation** | Status stored as field on `WorkflowState`; can drift from step results | `WorkflowState` — add `derive_status(steps)` pure function |
| **Per-step RBAC** | Single global `ServiceAccount` for all spawned pods | `KubernetesSpawner` + `PermissionScope` — create per-step SA + RoleBinding |
| **Secret volume mounts** | API keys passed as plain env vars | `WorkflowExecutor._execute_agent_step()` — use K8s Secret refs |
| **Idempotent create** | No dedup on workflow creation | `WorkflowExecutor.run()` — include definition hash in workflow ID |

### Code Quality Findings

#### PermissionScope defined but never enforced
`PermissionScope` (in `permissions.py`) defines `effective_tools()` for whitelist/blacklist filtering, but `create_generic_runner()` (in `generic_runner.py`) never calls it. Tool filtering doesn't actually happen at the permission layer.
- **Fix**: Call `scope.effective_tools(all_tool_names)` during tool registration in the runner

#### FilePersistence has no compare-and-swap
`FilePersistence` (in `persistence.py`) ignores the version field. Two concurrent writes silently overwrite each other. `PostgresPersistence` has CAS but `FilePersistence` does not.
- **Fix**: Implement atomic compare-and-swap using tempfile + rename + version check

#### Podman/K8s feature parity gap
`KubernetesSpawner` mounts ConfigMaps for agent config, registry, and tools. `PodmanSpawner` only supports volume mounts as a dict — no ConfigMap equivalent.
- **Fix**: Document the requirement or add a config-file abstraction that works on both targets

#### Definition snapshot not populated
`WorkflowState.definition_snapshot` is defined as `Optional[dict]` but the executor never populates it. On resume, there's no way to verify the workflow definition hasn't changed.
- **Operator pattern**: Each Result CR includes the step spec that was executed
- **Fix**: Populate at workflow start; validate on resume

#### parallel_group defined but unused
`WorkflowStepSpec.parallel_group` field exists but the executor only supports sequential execution.
- **Fix**: Either implement or remove to avoid confusion

### Test Coverage Gaps

| Component | What's Tested | What's Missing |
|---|---|---|
| **Spawner base** | Concurrency cap, active count | K8s/Podman implementations (no unit tests) |
| **Executor** | Single step, two steps, approval pause | Retry logic, resume after crash, approval timeout |
| **Conditions** | All operators and combinators | String escaping edge cases |
| **Persistence** | Save/load/delete/list | Concurrent writes to FilePersistence, CAS on Postgres |
| **Advisory** | Filtering, annotation | Integration with executor |
| **Permissions** | Whitelist, blacklist | Integration with generic_runner (never called) |
| **Tool loader** | Success path | ImportError, missing function, duplicate registration |
| **MCP loader** | Not tested | Auth resolution, missing env vars |
| **Escalation** | Not tested | EscalationHandoff format validation |
| **Remote client** | Basic calls | Polling timeout, circuit breaker |

### What Cloud Agents Does Better Than the Operator

| Cloud Agents Advantage | Why It Matters |
|---|---|
| **Podman spawner** | Product teams can deploy without Kubernetes |
| **YAML-driven agents** | No code changes to add/modify agents |
| **Advisory mode with tool filtering** | Safe exploration without side effects |
| **PostgreSQL persistence with CAS** | Horizontal scaling, crash recovery |
| **Structured escalation handoff** | Complete failure documentation for humans |
| **OpenTelemetry integration** | Distributed tracing across workflow → agent → LLM |
| **Condition evaluation (safe, no eval)** | Workflow branching without injection risk |
| **MCP server integration** | External tool access with auth — operator only has this as a CRD TODO |
| **Single runtime image** | One image runs any agent, vs three SDK runtimes in the sandbox |

---

## Improvement Roadmap

### Priority 1 — Security (required before production)

1. **Inject credentials via K8s Secrets**, not env vars (`WorkflowExecutor`, `KubernetesSpawner`)
2. **Add explicit `risk_level` to WorkflowStepSpec**, replace keyword-based classification (`WorkflowStepSpec`, `_classify_step_risk()`)
3. **Add auth to agent HTTP calls** — bearer token or SA token injection (`RemoteAgentClient`)

### Priority 2 — Robustness (from operator patterns)

4. **Add content-hash naming for spawned pods** — idempotent retry, safe re-execution (`KubernetesSpawner.spawn()`)
5. **Set owner references on spawned Jobs** — automatic GC on workflow deletion (`KubernetesSpawner`)
6. **Derive status from step results** — add `derive_status()` pure function, don't store status directly (`WorkflowState`)
7. **Wire PermissionScope into the runner** — actually enforce tool filtering (`create_generic_runner()`)
8. **Implement FilePersistence CAS** — atomic writes with version check (`FilePersistence`)

### Priority 3 — Completeness

9. **Add integration tests** for executor + spawner + client together
10. **Add K8s/Podman spawner unit tests** with mocked API clients
11. **Test approval timeout enforcement** (`WorkflowExecutor._check_approval_timeout()`)
12. **Populate definition_snapshot on workflow start** for resume safety (`WorkflowState`)
13. **Document Podman config file requirements** vs K8s ConfigMaps

### Priority 4 — Future capabilities

14. **Add optional CRD bridge** for OCP teams wanting kubectl/GitOps workflows
15. **Adopt OCI image volumes** for skills packaging (works on both K8s and Podman)
16. **Implement or remove parallel_group** from step specs
17. **Add console UI** or integrate with existing Lightspeed console plugin

## Recommendation

The Cloud Agents framework is the right foundation for multi-team adoption. The spawner abstraction, YAML-driven agent config, and Podman support address the fundamental requirements that the operator cannot meet.

The priority 1 security fixes should be addressed before any production deployment. The priority 2 robustness patterns from the operator are well-proven and straightforward to adopt — they address real failure modes (orphaned pods, status drift, duplicate spawns) that will occur in production.

The operator approach remains valid for the specific Lightspeed remediation use case on OCP, but the Cloud Agents framework is the generalized platform that other product teams can adopt.
