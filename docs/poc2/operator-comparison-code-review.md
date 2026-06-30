# Cloud Agents vs Operator — Code-Level Gap Analysis

**Date**: 2026-06-29
**Method**: Read all implemented Temporal code (`src/agents/workflow/temporal_*.py`, `src/agents/spawner/*.py`) and compared against latest `lightspeed-agentic-operator` upstream (`upstream/main` at `96771ce`) and `lightspeed-agentic-sandbox` (at `30aa46c`).

## Implementation Status

The Temporal-based Cloud Agents implementation is **fully functional** — not a stub. All core paths are implemented:

- Temporal workflow with sequential + parallel steps, conditions, approval signals, retry, escalation
- Activities that spawn K8s Jobs or Podman containers, call `POST /v1/agent/run`, destroy on completion
- Full sandbox HTTP contract (query, systemPrompt, outputSchema, context)
- Context building: targetNamespaces, previousAttempts, approvedOption, executionResult
- Env vars: `LIGHTSPEED_PROVIDER`, `LIGHTSPEED_MODEL`, provider-specific vars forwarded
- Skills mounting via OCI image (init container on K8s, volume trick on Podman)
- Advisory mode, notifications, cancellation, SSE events, definition store
- E2E tested on Kind and Podman with real Temporal Server

## Feature Parity Matrix

| Capability | Operator | Cloud Agents | Notes |
|---|---|---|---|
| Sandbox contract (`POST /v1/agent/run`) | ✅ | ✅ | Same contract |
| `LIGHTSPEED_PROVIDER` env var | ✅ | ✅ | |
| `LIGHTSPEED_MODEL` env var | ✅ | ✅ | |
| `LIGHTSPEED_PROVIDER_*` vars | ✅ | ✅ (forwarded from process env) | |
| Context: `targetNamespaces` | ✅ | ✅ | |
| Context: `previousAttempts` | ✅ | ✅ | |
| Context: `approvedOption` | ✅ | ✅ | |
| Context: `executionResult` | ✅ | ✅ | |
| Ephemeral pod per step | ✅ (SandboxClaim) | ✅ (K8s Job) | Different K8s primitives |
| Skills via OCI image | ✅ (native image volume) | ✅ (init container copy) | See gap #6 |
| Retry with escalation | ✅ | ✅ | |
| Approval gates | ✅ (ProposalApproval CRD) | ✅ (Temporal signal) | |
| Per-step agent selection | ✅ (per-step Agent CR ref) | ✅ (per-step in YAML) | |
| Workflow cancellation | ❌ | ✅ | Cloud Agents ahead |
| Advisory mode | ❌ | ✅ | Cloud Agents ahead |
| Podman support | ❌ | ✅ | Cloud Agents ahead |
| Parallel steps | ❌ | ✅ | Cloud Agents ahead |
| Flexible workflow (arbitrary steps) | ❌ (fixed 4-step) | ✅ | Cloud Agents ahead |
| YAML-driven agent config | ❌ (hardcoded Go templates) | ✅ | Cloud Agents ahead |
| Horizontal scaling | ❌ (single controller) | ✅ (Temporal workers) | Cloud Agents ahead |

## Gaps: What the Operator Does That Cloud Agents Doesn't

### Gap 1: Credential volume mount (High — blocks Vertex/Bedrock)

**Operator**: Uses both mechanisms:
- `addEnvFromSecret(tmpl, secretName)` — injects ALL keys from a K8s Secret as env vars (`envFrom.secretRef`)
- `addSecretVolume()` + `addVolumeMount()` — mounts the Secret at `/var/run/secrets/llm-credentials/` as files

**Cloud Agents**: Passes credentials as plain env vars from the worker process environment. The K8s spawner supports `SecretKeyRef` for individual env var keys, but does NOT:
- Mount the credential Secret as a volume at `/var/run/secrets/llm-credentials/`
- Use blanket `envFrom.secretRef` to inject all Secret keys

**Why it matters**: The sandbox's `config.py` sets `GOOGLE_APPLICATION_CREDENTIALS` to point to `/var/run/secrets/llm-credentials/GOOGLE_APPLICATION_CREDENTIALS` for Vertex and Bedrock providers. Without the volume mount, this file path doesn't exist, and file-based credential providers fail.

**Fix**: In `KubernetesSpawner.spawn()`, add:
1. A Secret volume mount at `/var/run/secrets/llm-credentials/` from the credential Secret
2. `envFrom.secretRef` to inject all Secret keys as env vars (covers providers that read env vars directly)

**Effort**: 1-2 days.

### Gap 2: MCP server injection (High — blocks extensibility)

**Operator**: Patches `LIGHTSPEED_MCP_SERVERS` as a JSON env var on the derived SandboxTemplate. For MCP headers that reference K8s Secrets, the operator mounts those Secrets at `/var/secrets/mcp/{server-name}/` and updates header configs to point to the mounted files.

```go
// From sandbox_templates.go
mcpServersEnvVar = "LIGHTSPEED_MCP_SERVERS"
```

**Cloud Agents**: Does not set `LIGHTSPEED_MCP_SERVERS` on spawned pods. MCP servers configured in the workflow definition are not passed to sandbox pods.

**Why it matters**: Without MCP, product teams can only use the sandbox's built-in tools (Bash, Read, Glob, Grep, Skill). MCP enables calling external tool servers (ServiceNow, PagerDuty, Jira, internal APIs) without changing the sandbox image. This is the primary extensibility mechanism.

**Fix**: 
1. Add `mcp_servers` field to `WorkflowInput` / step config
2. In the activity, serialize MCP server configs as JSON and set `LIGHTSPEED_MCP_SERVERS` env var
3. For MCP servers with Secret-based auth headers, mount those Secrets as volumes

**Effort**: 1 week.

### Gap 3: `LIGHTSPEED_MODEL_PROVIDER` not derived from workflow config (Medium)

**Operator**: Maps `LLMProvider.spec.type=GoogleCloudVertex` + `googleCloudVertex.modelProvider=Anthropic` → sets `LIGHTSPEED_MODEL_PROVIDER=anthropic` on the pod. This is computed from the CRD, not from env vars.

**Cloud Agents**: Forwards `LIGHTSPEED_MODEL_PROVIDER` from the worker's process environment to spawned pods. But the value is static — all workflows using Vertex get the same model provider. Can't vary per workflow (e.g., one workflow uses Claude on Vertex, another uses Gemini on Vertex).

**Fix**: Add `model_provider` field to `ProviderConfig`. The activity sets `LIGHTSPEED_MODEL_PROVIDER` from this field. Default to env var fallback for backward compat.

**Effort**: 1 day.

### Gap 4: Dynamic RBAC from agent output (Medium — acceptable for GA)

**Operator**: The analysis agent declares RBAC requirements in its output (`rbac.namespaceScoped`, `rbac.clusterScoped`). The operator creates:
- Per-proposal ServiceAccount
- Role + RoleBinding per target namespace
- ClusterRole + ClusterRoleBinding for cluster-scoped resources

All scoped to exactly what the agent declared, cleaned up on proposal completion via finalizers.

**Cloud Agents**: Uses a static ServiceAccount per spawner, configurable per step via `permissions.service_account`. No dynamic RBAC creation from agent output.

**Why it's acceptable for GA**: Product teams pre-create RBAC resources as part of deployment. The framework uses them. Dynamic RBAC is a hardening feature, not a launch blocker.

**Fix (post-GA)**: After the analysis step completes, the workflow reads the `rbac` field from the output, creates Roles/RoleBindings, sets the execution step's ServiceAccount to the newly created one, and cleans up on completion.

**Effort**: 2-3 weeks.

### Gap 5: `LIGHTSPEED_MODEL_PROVIDER` derivation for Vertex (Medium)

Covered by Gap 3 — same issue.

### Gap 6: Skills via native K8s image volumes (Low)

**Operator**: Uses native K8s image volumes (`volumes[].image.reference`) which mount OCI images directly as read-only volumes. Supports `subPath` for mounting specific skill directories.

**Cloud Agents**: Uses an init container that copies from the skills image to an emptyDir volume. The main container mounts the emptyDir at `/app/skills`.

**Trade-offs**:
- Init container: Works on K8s < 1.31, adds 5-10s startup latency, no subPath support
- Native image volumes: Requires K8s 1.31+ (OCP 4.18+), instant mount, subPath support

**Fix**: Add native image volume support as an option, with init container as fallback for older K8s versions. The spawner checks API server version or reads a config flag.

**Effort**: 1 week.

### Gap 7: Template reuse / content-hash dedup (Low)

**Operator**: Computes a SHA256 hash of the full step config (agent, model, tools, secrets, base template ResourceVersion) and creates a derived SandboxTemplate only when the hash is new. Old templates for the same agent+step are garbage-collected.

**Cloud Agents**: Creates a fresh K8s Job per step with a content-hash name (`workflow_id + step_name + attempt`). No template reuse across workflows.

**Impact**: More K8s objects per workflow. Negligible at expected volumes (<100 concurrent workflows). Only matters at scale.

**Fix (post-GA)**: Cache derived pod specs by config hash. Reuse for identical configurations. GC old entries.

**Effort**: 1 week.

## Prioritized Fix List for Phase 3

| # | Gap | Severity | Phase 3 Task | Effort |
|---|---|---|---|---|
| 1 | Credential volume mount | High | New task needed | 1-2 days |
| 2 | MCP server injection | High | New task needed | 1 week |
| 3 | `LIGHTSPEED_MODEL_PROVIDER` derivation | Medium | New task needed | 1 day |
| 4 | Dynamic RBAC | Medium | Post-GA (backlog) | 2-3 weeks |
| 6 | Native K8s image volumes | Low | Post-GA (backlog) | 1 week |
| 7 | Template reuse | Low | Post-GA (backlog) | 1 week |

Gaps 1-3 should be added to Phase 3 tasks. Gaps 4, 6, 7 go to backlog.
