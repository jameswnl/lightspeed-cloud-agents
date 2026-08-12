# Implementation Plan — CODE-REVIEW.md Remediation

Prioritized plan to address the findings in [CODE-REVIEW.md](CODE-REVIEW.md).

Ordering principle: close exploitable holes first (P0), then claimed-but-missing
guardrails (P1), then robustness/scale and doc reconciliation (P2), then polish (P3).
Per repo convention, every new guardrail gets its test written first and a row added
to the CLAUDE.md guardrail table.

Verified against current code before planning: auth defaults to off
(`temporal_entrypoint.py:159`, `authorization.py:180`), `skills_paths` is interpolated
into `sh -c` (`kubernetes_spawner.py:169,186`), there is no image allowlist anywhere
under `src/cloud_agents/`, and `redact.py` is only used in `temporal_activities.py`.

---

## P0 — Exploitable security holes (do first, ~1 week)

### Task 1: Fix `skills_paths` command injection (review #3)

Smallest fix, highest payoff.

- In all three spawners (`kubernetes_spawner.py:169,186`, `podman_spawner.py:105,108`,
  `openshell_spawner.py:731,737`), replace the `" && ".join(f"cp -r {p} ...")` +
  `sh -c` pattern with argv-style exec: `["cp", "-r", *paths, "/skills-data/"]`
  (or one `cp` per path without a shell).
- Add input validation in `temporal_validation.py`: reject `skills_paths` entries that
  aren't absolute paths matching `^/[A-Za-z0-9._/-]+$`.
- Give the K8s init container the same `securityContext` as the main container
  (`kubernetes_spawner.py:182-194`).
- **Tests:** injection payloads rejected at `/run`; spawner unit tests assert no
  `sh -c` in built command and assert init-container securityContext
  (`spawner/test_kubernetes_spawner.py`, `test_podman_spawner.py`); add E2E case to
  `e2e/test_guardrails.py`.

### Task 2: Image + ServiceAccount allowlists (review #2)

- New env/config: `SANDBOX_IMAGE_ALLOWLIST` and `SERVICE_ACCOUNT_ALLOWLIST`
  (comma-separated; support registry/repo prefix globs like `quay.io/lightspeed/*`).
- Enforce at `/run` in `temporal_validation.py` (422 on violation) *and*
  defense-in-depth in `SpawnConfig` validation in `spawner/base.py`, so the activity
  path can't bypass the API check.
- Fail-closed decision: empty allowlist = deny non-default images/SAs (allow only the
  configured defaults), consistent with the risk classifier's fail-closed posture.
- **Tests:** validation cases for allowed/denied/glob; `spawner/test_base.py` for the
  SpawnConfig check; audit event emitted on denial.

### Task 3: Secure-by-default auth (review #1)

- Flip defaults: `AUTH_REQUIRED` default `true` (`temporal_entrypoint.py:159`),
  `WORKFLOW_AUTHZ` default from `none` to `policy` (`authorization.py:180`) — or at
  minimum make `none` log a loud startup warning and require an explicit opt-out
  (e.g., `ALLOW_INSECURE=true`) to boot unauthenticated.
- Update Helm values (`workflowRunner.*`), DEPLOYMENT.md, and example env files so dev
  setups still work with one documented flag.
- **Tests:** entrypoint boots fail-closed with no env set; opt-out flag works;
  `temporal/test_api.py` default-deny coverage.

### Task 4: Path traversal in CLI-handoff (review #7)

Trivial.

- Slugify/validate `workflow_id` before building `filename` in
  `escalation.py:383-386` (e.g., reject anything not `^[A-Za-z0-9_-]+$`, or hash it),
  and/or validate `request.workflow_id` at `temporal_api.py:327`.
- **Tests:** `../../etc/cron.d/x` style IDs rejected or sanitized.

---

## P1 — Claimed guardrails that don't exist yet (~1–2 weeks)

### Task 5: NetworkPolicy in `KubernetesSpawner` (review #5)

This is what makes Task 2's RCE exploitable for exfiltration.

- Port the NetworkPolicy construction from `openshell_spawner.py:250` into
  `KubernetesSpawner.spawn()`: default-deny egress plus allowances for DNS, the LLM
  provider endpoint, and configured MCP servers; delete it in `destroy()` and in
  orphan reconciliation.
- **Tests:** `spawner/test_kubernetes_spawner.py` asserts policy created/deleted with
  the pod; E2E case in `test_guardrails.py -k kind`.

### Task 6: Extend redaction to transcripts and escalation packages (review #6)

- Apply `redact.py` at transcript write time (before PostgreSQL persistence) and at
  serve time for `/transcript` and `/handoff`, plus to escalation package assembly in
  `escalation.py`. Redact-on-write is the priority; serve-time is belt-and-braces for
  existing rows.
- Seed the redactor with the known credential values from the request/env
  (exact-match redaction), not just regex patterns.
- **Tests:** transcript containing a known secret comes back redacted from both
  endpoints; escalation markdown redacted.

### Task 7: Credential isolation on K8s — reconcile or fix (review #4)

- Decision point (bring to the team): either
  - **(a)** accept env-var injection on K8s/Podman and re-scope the R8 claim in
    `architecture-visualization.html` to OpenShell only — a docs fix; or
  - **(b)** implement egress-proxy credential injection for K8s like OpenShell has.
- Recommendation: (a) for now, with (b) tracked in `productization-roadmap.md` as P1.

### Task 8: TokenReview blocking I/O (review #8)

- In `auth.py:236-250`: load incluster config once at startup, cache the client, and
  run `create_token_review` via `asyncio.to_thread` (or the async k8s client). Add a
  small TTL cache on token→identity to cut per-request reviews.
- **Tests:** mock-based test asserting no client construction per request;
  loop-blocking regression test if feasible.

---

## P2 — Functional gaps & scale (roadmap items, ~1–2 weeks)

### Task 9: Retry adaptation for R3

- Replace reliance on Temporal `RetryPolicy` for agent steps with a workflow-level
  retry loop in `temporal_workflow.py` (`max_retries` from the YAML): on failure,
  append the prior attempt's error/summary to the next attempt's prompt context, then
  escalate after exhaustion. Keep `RetryPolicy` for infrastructure-level failures
  (spawn errors) only.
- **Tests:** `temporal/test_workflow.py` — second attempt's prompt contains first
  attempt's failure; escalation only after N attempts.

### Task 10: Shared state for rate limit / dedup / circuit breaker (R5)

- Add an optional Redis backend behind small interfaces for `rate_limiter.py`, alert
  dedup (`alert_trigger.py:197`), and the circuit breaker
  (`temporal_activities.py:40`), keeping in-memory as the single-replica default.
- Largest item; can trail the others. Land the docs caveat immediately (Task 12).

### Task 11: SSE endpoint hardening (low-sev)

- Add max stream lifetime (e.g., 30 min), client-count cap, and back-off polling in
  `temporal_api.py:742-780, 924-938`.

### Task 12: Documentation reconciliation

Cheap — do alongside P0/P1.

- Update `architecture-visualization.html` FAQ/requirement cards:
  - scope the R8 credential claim to OpenShell,
  - remove/annotate the K8s NetworkPolicy claim until Task 5 lands,
  - soften the R3 retry-adaptation claim until Task 9 lands,
  - note per-replica limits vs R5,
  - state the auth default explicitly.
- Per CLAUDE.md: verify every claim against code, keep the HTML FAQ verifiable.

---

## P3 — Quality polish (fill-in work, ~2–3 days)

- **Condition parser:** tokenize instead of splitting on literal `" or "` / `" and "`
  (`conditions.py:37-42`); add quoted-string test cases.
- **`WorkflowStepSpec` cleanup:** remove dead fields (`spawn`, `agent`,
  `spawn_config`, top-level `service_account`), set `extra="forbid"`; update
  `test_no_dead_fields` and all example YAMLs (`test_example_definitions.py` will
  catch stragglers).
- **`except (json.JSONDecodeError, Exception)`** → `except Exception`
  (`temporal_activities.py:231`).
- **File-handle leaks** in `_build_tls_config` (`temporal_entrypoint.py:59-61`) →
  context managers.
- **`{{ input }}` matching:** use a regex tolerant of whitespace
  (`temporal_workflow.py:410`) — and while there, fix the low-sev injection nuance by
  substituting `{{ input }}` *after* the templating pass with the same `<data>`
  wrapping as other values.

---

## Suggested sequencing

| Week | Tasks |
|------|-------|
| 1 | Tasks 1, 4, 3, 2 (P0) + Task 12 doc edits |
| 2 | Tasks 5, 6, 8, and the Task 7 decision |
| 3+ | Tasks 9, 11, P3 batch, then Task 10 as the standing roadmap item |

Each task is independently landable; nothing blocks another except Task 12, which
should trail whatever it documents.
