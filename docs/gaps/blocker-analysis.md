# Technical Blocker Analysis

Deep analysis of all open tasks for hidden technical blockers, dependency risks, and complexity traps.

**Independently evaluated** — findings cross-checked by an opus evaluator who challenged, found missing blockers, and corrected estimates.

## Methodology

For each task: what's the hardest part, what could block implementation, what depends on external systems, and what's the real effort vs stated effort.

---

## Potential Blockers Found

### BLOCKER 1: T1 (PermissionScope) — sandbox contract extension is an upstream dependency

**Task**: T1 forwards `allowedTools`/`deniedTools` to the sandbox POST body. Runner-side forwarding is DONE. But the sandbox doesn't consume these fields.

**Blocker**: The security gap (per-step tool filtering) cannot be closed without upstream changes to `lightspeed-agentic-sandbox`. The sandbox uses the OpenAI agents SDK which has its own tool registration. Injecting tool filtering from an external request field may require:
1. The sandbox to intercept tool registration and filter based on `allowedTools`/`deniedTools`
2. Or the sandbox to wrap the agent runner with a tool filter middleware
3. Or a contract change where the sandbox's `/v1/agent/run` API accepts and enforces these fields

**Risk**: The sandbox team may not accept this contract extension, or the OpenAI agents SDK may not support dynamic tool filtering at the level needed.

**Mitigation**: Verify with sandbox team whether this is feasible before counting on it.

### BLOCKER 2: T36 (Progress Streaming) — fundamentally changes the execution model

**Task**: Stream agent work-in-progress to callers via a side channel.

**Blocker**: This is not just a feature addition — it introduces a new runtime component (ProgressStore), a new network path (sandbox → runner callback), a new auth mechanism (per-step tokens), and breaks the stateless runner assumption. The design went through 2 review rounds and was explicitly deferred because:
1. In-memory ProgressStore breaks multi-replica statelessness
2. Callback URL addressing varies per deployment target
3. Sandbox-side streaming support doesn't exist yet (upstream dependency)
4. Event schema undefined

**Risk**: This could become a multi-month effort if multi-replica support is required from the start, or if the sandbox streaming contract is contentious.

**Mitigation**: Accept single-replica limitation for v1. Separate runner-side and sandbox-side work.

### BLOCKER 3: T11 (Agents-as-Tools) — requires defining the tool generation contract

**Task**: Registry auto-generates LLM tool definitions from workflow definitions.

**Blocker**: The DefinitionStore has `list_all()` and `get()`, but generating useful LLM tools from workflow definitions requires:
1. Defining what parameters a tool exposes (which step inputs are tool parameters?)
2. Defining how tool return values map to workflow outputs
3. Deciding how async workflows map to synchronous tool calls (start + poll? block until done?)
4. Integration with whatever chatbot/LLM framework consumes the tools

**Risk**: This is a design problem, not an implementation problem. Without a clear tool generation spec, implementation will stall or produce something unusable.

**Mitigation**: Write a tool generation design doc before implementing. Define the exact tool schema for one concrete workflow.

### BLOCKER 4: T9 (Dynamic RBAC) — requires K8s controller-like behavior in an HTTP service

**Task**: After an analysis step, read `rbac` field from output, create per-proposal Roles/RoleBindings, clean up on completion.

**Blocker**: This requires the workflow runner to:
1. Create K8s RBAC resources dynamically (needs elevated permissions)
2. Bind them to the correct ServiceAccount for the next step
3. Clean up on workflow completion (finalizer-like behavior)
4. Handle partial failures (RBAC created but step fails — who cleans up?)

**Risk**: This is the operator's pattern (CRD controller with finalizers), transplanted into an HTTP service without the K8s controller machinery. The lifecycle management is the hard part, not the RBAC creation.

**Mitigation**: Defer until CRD operator (T35) is considered. Or implement a simpler version that uses pre-existing RBAC rather than creating it dynamically.

### BLOCKER 5: T8 (Per-sandbox Identity Binding) — requires ServiceAccount lifecycle management

**Task**: Generate scoped ServiceAccounts per sandbox spawn; verify identity matches.

**Blocker**: Creating a ServiceAccount per sandbox pod means:
1. The runner needs `create` permission on ServiceAccounts (elevated RBAC)
2. The SA must be created before the Job, and cleaned up after
3. The SA token must be mounted into the sandbox pod
4. The sandbox must use this token when calling back to the runner
5. Race conditions: SA not ready when Job starts, SA leaked if Job creation fails

**Risk**: Similar to T9 — K8s resource lifecycle management without controller machinery. The cleanup path is the hardest part.

**Mitigation**: Use projected SA tokens (already partially implemented) rather than creating new SAs. Or scope to per-namespace pre-created SAs rather than per-pod dynamic SAs.

---

## Dependency Chains

```
T1 (forwarding done) → upstream sandbox enforcement (EXTERNAL BLOCKER)
T11 (agents-as-tools) → T12 (chatbot trigger) → T16 (conversational approval)
T36 (streaming) → T27 (resumable SSE)
T2 (timeout) ← may be superseded by T36 (streaming changes the HTTP model)
T9 (dynamic RBAC) ← probably needs T35 (CRD operator) first
T8 (per-sandbox identity) ← needs K8s SA lifecycle design
T40 (prompt injection) ← needs design discussion, no clear path yet
```

## Tasks With No Hidden Blockers (safe to implement)

| Task | Why it's safe |
|------|---------------|
| T2: Timeout termination | Self-contained, well-understood Temporal SDK feature |
| T13: Alert trigger | Standard webhook endpoint, Alertmanager format is well-documented |
| T14: Schedule trigger | Temporal has native cron, just API exposure |
| T23: Rate limiting | Standard FastAPI middleware, well-understood |
| T37: Secret redaction | String filtering, no external dependencies |
| T38: Request body size | FastAPI middleware config, trivial |
| T39: Egress enforcement | Helm default change, no code |
| T42: Token rotation | Add multi-token support, straightforward |
| T43: Definition content policy | Pydantic validators, self-contained |
| T5: Doc completeness | Doc-only |
| T18: Operational runbooks | Doc-only |

## Evaluator Findings (independent review)

### MISSING BLOCKER 6: Temporal SDK version floor is dangerously loose

`pyproject.toml` specifies `temporalio>=1.9.0` with no upper bound. The Temporal Python SDK has breaking changes between minor versions. A fresh `uv sync` could pull a newer version with incompatible workflow serialization, activity registration, or interceptor APIs. This silently affects T2, T14, T25, T34 — every task that touches Temporal.

**Mitigation**: Pin to a specific minor version (e.g., `temporalio>=1.9.0,<1.10`).

### MISSING BLOCKER 7: T7 RBAC depends on Temporal query for authz context — fragile integration

T7 is marked DONE, but `approve`/`cancel` authorization depends on querying `AgentWorkflow.get_authz_context` from Temporal. If the query fails or returns stale data (Temporal server upgrade, workflow eviction), authorization silently breaks. The fail-closed fix (503 on query failure) handles crashes but not stale/missing data.

### CROSS-CUTTING RISK: Auth architecture affects T8, T36, T42 together

Three tasks independently touch authentication:
- T8 (per-sandbox identity) — tightens TokenReview path
- T36 (per-step progress callback tokens) — introduces a third auth path
- T42 (token rotation) — changes the Bearer path

These should be designed as a unified auth evolution, not three independent tasks.

### CORRECTION: T36 should not be Phase 1

T36 is a 4-6 week effort with upstream dependencies and open architecture questions. The existing SSE polling (`temporal_api.py` lines 391-442) delivers step-level visibility today. T36 adds token-level streaming — valuable but not a prerequisite for anything in Phase 1. Recommendation: move to Phase 3.

### CORRECTION: T1 has a simpler interim mitigation

The runner could validate tool usage in the *response* — if the sandbox output indicates a denied tool was called, the runner rejects the step. Advisory enforcement without upstream changes. Not as strong as sandbox-side blocking, but closes the audit gap immediately.

### CORRECTION: T37 (secret redaction) is understated

Listed as 1 day but requires tracking which env vars contain secrets through the spawner into activity error handlers. Closer to 2-3 days with tests.

### CORRECTION: T9 is already Phase 4 — calling it a blocker is misleading

The complexity is real but it's already deprioritized. Blocker analysis should focus on Phase 1-3 items.

## Tasks That Are Larger Than They Look

| Task | Stated effort | Real effort | Why |
|------|--------------|-------------|-----|
| T8: Per-sandbox identity | 1-2 weeks | 3-4 weeks | K8s SA lifecycle management, race conditions, cleanup |
| T9: Dynamic RBAC | 2-3 weeks | 4-6 weeks | Controller-like behavior without controller infra |
| T11: Agents-as-tools | 2-3 weeks | 4-6 weeks | Design problem, not implementation. Tool generation spec undefined |
| T15: CLI handoff | 1-2 weeks | 3-4 weeks | Integration with external CLI tools (Claude Code, Goose), context serialization format undefined |
| T36: Progress streaming | 1-2 weeks | 4-6 weeks | New runtime component, cross-repo contract, statelessness compromise |
| T40: Prompt injection | TBD | 2-4 weeks | Research + design + integration with guardrail framework |

## Recommended Immediate Actions

1. **Pin Temporal SDK version** — zero cost, prevents silent breakage across all Temporal tasks
2. **Move T36 from Phase 1 to Phase 3** — existing SSE polling is sufficient; T36 is a multi-month effort with upstream deps
3. **Design auth evolution holistically** — write one auth design doc covering T8, T36, T42 before implementing any individually
4. **Add advisory tool enforcement for T1** — runner-side response validation as interim mitigation while waiting on sandbox upstream
5. **Tackle safe quick wins next** — T37 (secret redaction), T38 (body size limits), T39 (egress default), T14 (schedule trigger) are all low-risk, high-value, and unblocked
