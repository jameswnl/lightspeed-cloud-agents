# Technical Blocker Analysis — Remaining Tasks

**Date**: 2026-07-01
**Method**: Deep analysis of each remaining task against the implemented codebase, with independent evaluation. Cross-referenced against lightspeed-agentic-operator patterns.

## Ranked Risk Assessment

### LIKELY BLOCKERS

#### T36: Agent Progress Streaming — HIGHEST RISK

**Why**: This requires changes to the sandbox HTTP contract (`progressUrl`, `progressToken` fields) — a cross-team, cross-repo dependency. The sandbox team needs to extend the OpenAI Agents SDK streaming to emit callback POSTs, add bearer token handling on outbound calls, and handle failure modes. This is the longest lead-time item.

**Hidden issues**:
- **Dual-deployment fragility**: K8s callback URL works via Service DNS. Podman uses `host.containers.internal` which doesn't work on all Linux distros. The Podman path needs prototyping before committing.
- **Stateless violation**: In-memory `ProgressStore` breaks the stateless runner contract (R7). SSE clients must connect to the same replica that spawned the sandbox, requiring sticky sessions or Redis. Neither is in the current architecture.
- **T2 ordering conflict**: T2 (heartbeat/timeout) notes that "T36 may change the sync HTTP model." These tasks have an unacknowledged mutual dependency. T2's heartbeat design should anticipate the side channel, or T36 should ship first.

**Action**: Get sandbox team alignment on the contract extension NOW. Resolve T2/T36 ordering. Prototype Podman callback path. Decide single-replica vs Redis upfront.

#### T11: Agents-as-Tools — LIKELY BLOCKER

**Why**: The sync/async impedance mismatch is unsolved. LLM tool calls expect synchronous responses. Temporal workflows are async — they pause for approval, retry, and can run for minutes. Options:
- Block until complete (ties up LLM context for minutes)
- Return "started" + poll (breaks standard tool call patterns)
- Restrict to auto-approved fast workflows only

None of these are mentioned in the task description, and each has different architectural implications.

**Hidden issues**:
- **Schema translation**: Workflow `WorkflowInput` has 13 fields. Which become tool parameters? The mapping is non-obvious and different per workflow.
- **No consumer without T12**: T12 (chatbot trigger) depends on T11, but T11 needs T12 to be useful. And T12 depends on LCS integration (external team).

**Action**: Narrow scope — start with manually-registered tools for specific workflows, not auto-generation. Solve the async pattern for one workflow first. "2-3 weeks" is only realistic for a restricted-scope implementation.

#### T35: CRD-based K8s Operator — LIKELY BLOCKER (misestimated)

**Why**: "Thin CRD-to-executor bridge" is an iceberg. The existing agentic operator has 15+ type files, reconcilers, finalizers, owner references, CEL validation, and e2e tests. A "bridge" still needs:
- CRD types (Go structs, deepcopy, generated manifests)
- Reconciler watching CRDs, calling Cloud Agents API
- Status sync (Temporal state → CRD status)
- Cleanup via finalizers
- RBAC mapping (K8s RBAC ↔ PolicyFileAuthorizer)

**Action**: Reestimate as 6-8 weeks, not a backlog item. Consider whether the existing `lightspeed-agentic-operator` could be refactored to call the Cloud Agents API instead.

### HIDDEN COMPLEXITY

#### T8: Per-Sandbox Identity Binding

**Why**: Creating a ServiceAccount + RoleBinding per spawn doubles the K8s API surface (2 extra creates, 2 extra deletes per step). For parallel workflows, this multiplies.

**Hidden issues**:
- **Podman gap**: ServiceAccounts don't exist in Podman. No equivalent mentioned. Feature is K8s-only.
- **Cleanup on failure**: If SA + RoleBinding are created but the Job fails to start, they're orphaned. Current orphan reconciliation only looks for Jobs, not RBAC resources.
- **Token propagation timing**: Projected SA tokens need the pod running before the token is available, but the spawner waits for readiness via HTTP. New lifecycle phase needed.

**Action**: Design cleanup path first (extend orphan reconciliation). Accept K8s-only. Benchmark K8s API overhead with parallel workflows.

#### T15: Interactive CLI Handoff

**Why**: "Package context and launch CLI" hides two hard problems:
- **Environment transfer**: The sandbox has cluster credentials via env vars and volume mounts in an ephemeral container. A CLI session runs on a human's machine — completely different credential delivery.
- **Session lifecycle**: Does the framework actually launch a CLI process (interactive, long-running — opposite of ephemeral model), or just generate a command the human copy-pastes? The former is 2+ weeks with security implications; the latter is 2-3 days.

**Action**: Start with "generate a launch command with pre-loaded context." This solves context packaging without the session lifecycle problem.

#### T9: Dynamic RBAC from Agent Output

**Why**: The operator's pattern (`rbac.go`) works because it's a K8s controller with direct API access and owner references for GC. In Cloud Agents:
- The runner needs elevated K8s RBAC itself (create Roles, RoleBindings) — expanding its security surface
- No owner references for GC — cleanup depends on `finally` blocks which have known reliability issues
- The sandbox response schema has no RBAC field

**Action**: Defer until T8 is done (dynamic RBAC builds on per-sandbox SAs). Design RBAC response schema extension first.

#### T25: Nested Workflows

**Why**: Temporal supports child workflows, but Cloud Agents routes everything through `run_sandbox_step`. A nested workflow either bypasses the sandbox (the workflow class needs a new step type) or creates a circular dependency (sandbox calls back to the runner API).

**Hidden issues**:
- Resource exhaustion: 3-step workflow nesting 3-step workflow = 6+ pods. `MAX_SPAWNED_PODS` is global, no per-workflow budget.
- Approval propagation: If nested workflow hits an approval gate, does the parent block? Who approves?

**Action**: Use Temporal's native child workflow. Implement resource budgeting per workflow tree. Define approval propagation semantics before implementation.

#### T12: Chatbot Trigger (LCS integration)

**Why**: Double-blocked. Depends on T11 (agents-as-tools, itself a likely blocker) AND on LCS `/query` integration (external team, unknown scope). Effort is "TBD" for good reason.

**Action**: Don't plan until T11 is complete and LCS integration surface is documented.

### LOW RISK

| Task | Why low risk | Effort accurate? |
|---|---|---|
| **T2**: Sandbox termination/heartbeat | Straightforward Temporal API. Caveat: resolve T36 ordering first. | 1 day ✓ (if T36 ordering resolved) |
| **T13**: Alert trigger | New endpoint, no cross-team dependency, same on K8s and Podman | 1 week ✓ |
| **T14**: Schedule trigger | Temporal native `cron_schedule` param. Pass-through. | 2-3 days ✓ (possibly generous) |
| **T23**: Rate limiting | Standard FastAPI middleware. `CallerIdentity` already available for per-user keying. | 2-3 days ✓ |
| **T37-T43**: Security hardening | All internal, no cross-team deps. Individually straightforward. Collectively ~1-2 weeks. | Realistic |

## Priority Actions

1. **Get sandbox team alignment on T36 contract extension NOW.** Cross-team contract negotiation is the longest lead-time item. Don't wait until Phase 3 starts.

2. **Resolve T2/T36 ordering.** Pick one to go first and design it to accommodate the other. Current plan has both in Phase 3 with no stated order.

3. **Narrow T11 scope aggressively.** Solve the sync/async pattern for one concrete workflow before attempting auto-generation.

4. **Reestimate T35.** It's a multi-month project. Either budget accordingly or repurpose the existing operator.

5. **Accept K8s/Podman feature divergence for T8 and T9.** Per-sandbox identity and dynamic RBAC are K8s-only. Trying to find Podman equivalents will stall both tasks.

6. **Define T15 as "generate launch command" not "launch interactive session."** The former is days; the latter is weeks with security implications.
