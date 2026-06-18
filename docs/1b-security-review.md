# Review: Phase 1b Security

## Findings

### 1. Blocker: new internal agent APIs are introduced without any inter-agent authentication model

Phase 1b adds new internal agent APIs:

- `POST /v1/run`
- `GET /v1/runs/{run_id}`
- `/metrics`
- `/livez`

It also adds autonomous cross-pod dispatch and async polling, but the plan still defers RBAC and NetworkPolicy. That means there is no stated authentication or authorization model between agent pods, even though the async polling API exposes stored run results for up to an hour.

#### Why this matters

Without a defined trust boundary, any workload that can reach the agent pod network path may be able to:

- submit new runs
- poll prior run results
- scrape metrics
- probe liveness/readiness

Even for a PoC, this is a real security property, not just a future hardening concern.

#### Recommendation

At minimum, Phase 1b should explicitly define one of these:

- **Trusted-dev-only boundary:** agent endpoints are internal-only and must never be exposed outside the cluster/dev network
- **Minimal auth boundary:** agent-to-agent calls require a shared token or mTLS-like trust mechanism

If you want to keep full RBAC/NetworkPolicy out of scope, the plan should still document the intended trust model so the new APIs are not implicitly unauthenticated by accident.

### 2. Major: the plan regresses from the broader cloud-agents security direction by deferring ServiceAccounts and NetworkPolicy while increasing attack surface

The broader roadmap already framed dedicated ServiceAccounts and NetworkPolicy as part of the agent-pod design. Phase 1b expands the surface area with:

- a second autonomous pod
- periodic dispatch
- async stored run state
- metrics endpoints

but explicitly defers NetworkPolicy and ServiceAccount RBAC to Phase 2.

#### Why this matters

That creates an awkward mismatch:

- the architecture says agent pods should be isolated by role
- the Phase 1b plan increases the number of privileged network/API paths
- the actual hardening is deferred

This is especially risky because autonomous agents are not purely request/response services; they initiate traffic on their own.

#### Recommendation

If full security hardening truly must wait, at least add a **minimum containment baseline** for Phase 1b:

- monitoring pod may call diagnostic pod, but not vice versa
- agent Services are cluster-internal only
- no ingress exposure
- clearly documented "do not deploy outside dev/test clusters" note

That keeps the scope manageable without pretending the surface area has not changed.

### 3. Major: async run polling creates a new data exposure surface, but run IDs are treated as sufficient protection

The async design stores `RunState` objects in memory for one hour and exposes them via `GET /v1/runs/{run_id}`.

#### Why this matters

If `run_id` is the only access control:

- whoever knows or guesses a run ID can read its result
- correlation between monitoring and diagnostic runs becomes observable
- error payloads or outputs may leak operational details

Even UUIDs are identifiers, not authorization.

#### Recommendation

For Phase 1b, define one of these:

- run polling is only allowed from the originating caller context
- polling requires the same auth material as submit
- run IDs are scoped and not treated as secret-bearing credentials

If none of that is in scope, the plan should explicitly note that async polling is a trusted-internal-only interface.

### 4. Major: correlation IDs and logs improve observability, but they also create a new data-handling surface with no stated sanitization rules

The plan says correlation IDs are added to request context, echoed in response headers, and included in every log line alongside `agent_name` and `run_id`.

#### Why this matters

This is useful, but it introduces security/logging questions:

- are caller-supplied correlation IDs trusted or normalized?
- can malicious or malformed values poison logs?
- can correlation IDs be used to join data across runs and endpoints unexpectedly?

If these IDs are propagated blindly from request context into logs and headers, they become part of the externally influenced logging surface.

#### Recommendation

Define basic rules up front:

- if absent, generate a server-side UUID
- if present, validate/normalize length and allowed characters
- never log arbitrary nested context blobs directly

That is a small amount of work and closes off easy log-injection or cardinality problems.

### 5. Medium: per-tool metrics risk exposing sensitive operational shape unless label cardinality and naming are explicitly bounded

The plan wants metrics such as:

- `ls_agent_runs_total`
- `ls_agent_run_duration_seconds`
- `agent_tool_calls_total{agent_name, tool_name}`

#### Why this matters

Per-tool metrics can unintentionally reveal:

- what remediation tools exist
- which tools are being used most often
- operational patterns of failures and responses

This may be acceptable internally, but the plan should treat it as an exposure decision, not a free byproduct.

#### Recommendation

Constrain this deliberately:

- keep labels bounded
- avoid labels derived from hostnames, prompts, or user-supplied context
- document `/metrics` as internal-only

If tool names are considered sensitive in some environments, consider deferring per-tool metrics and keeping only aggregate per-run counts.

## Open Questions / Assumptions

1. Are Phase 1b agent endpoints intended to be strictly internal to a dev/test cluster, with no external ingress?
2. Is `run_id` meant to function only as a lookup key, or is it implicitly being treated as an authorization secret?
3. Will caller-provided `correlation_id` values be accepted as-is, or normalized server-side?
4. Is there any minimum security baseline you are willing to include in Phase 1b short of full RBAC and NetworkPolicy?

## Summary

The biggest security issue in Phase 1b is not a specific code bug; it is that the phase materially increases the internal API and communication surface while still treating security as mostly deferred work.

That can be acceptable for a tightly-scoped PoC, but only if the plan says so explicitly and defines a minimum trust model. Right now the design is strong on runtime behavior and observability, but under-specified on who is allowed to call what, who can read run results, and how much internal operational detail is intentionally exposed.

If I were tightening one thing first, I would add a clear trust-boundary section for Phase 1b:

- who may call agent endpoints
- whether polling endpoints are protected
- whether `/metrics` is internal-only
- what minimum containment applies in Kind/Podman and OCP

That would make the phase much safer to execute without forcing full production-grade security into this iteration.
