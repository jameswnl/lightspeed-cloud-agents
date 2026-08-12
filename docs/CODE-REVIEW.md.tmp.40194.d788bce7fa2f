# Lightspeed Cloud Agents — Code Review

**Scope:** Full implementation (~11k lines) under `src/cloud_agents/` — spawners, the
Temporal workflow engine, auth/authz, triggers, and supporting modules — evaluated
against the goals (G1–G5) and requirements (R1–R12) in
`docs/architecture-visualization.html`.

**Perspectives:** Functional, Quality, Security.

---

## Summary verdict

This is a well-structured, thoughtfully-built PoC. The architecture is sound (a
generic YAML-interpreting Temporal workflow, a clean spawner abstraction, durable
state, good observability), most requirements are genuinely met, and the code is
readable, typed, and consistently documented, with tests mapped to guardrails.

The main concern is a **gap between the security posture the doc advertises and what
the default / Kubernetes code path actually enforces.** Several "guardrails" are
opt-in or implemented in only one of the three spawners, and the primary trust
boundary — who may spawn what container with what identity — is essentially
unguarded. For a PoC exploring requirements this is reasonable, but the visualization
presents these as delivered controls, so the discrepancies are flagged below.

---

## Security perspective

### High severity

**1. Fully open by default (`AUTH_REQUIRED=false`, `WORKFLOW_AUTHZ=none`).**
`temporal_entrypoint.py:159,171` returns no auth dependency unless
`AUTH_REQUIRED=true`, and `authorization.py:180` treats missing authz as
"anonymous allow." Out of the box every endpoint — `/v1/workflows/run`,
`/v1/cli-sessions`, the Alertmanager webhook — is unauthenticated and unauthorized.
R8 states "Authenticated access on all endpoints" and R9 "Fail-closed when enabled";
the shipped default is fail-*open*.
*Fix:* default to secure, and state clearly that security is opt-in if that is
intended.

**2. Arbitrary container image + arbitrary ServiceAccount per request → code
execution in the cluster.**
`RunWorkflowRequest.sandbox_image` (`temporal_api.py:69`) is a free-form string with
no allowlist (confirmed: no `image_allowlist` / `ALLOWED_IMAGE` anywhere), and each
step's `permissions.service_account` (`temporal_activities.py:378-380`) flows
straight to `service_account_name` in the pod spec (`kubernetes_spawner.py:342`) with
no validation. Any caller authorized to TRIGGER can run any image under any SA in the
runner's namespace. The pod `securityContext` (non-root, read-only, no-priv-esc) and
`automount_service_account_token=False` limit the blast radius, but arbitrary-image
execution with unrestricted egress (see #5) is effectively RCE inside the cluster.
*Fix:* image allowlist + SA allowlist, validated at `/run`.

**3. Command injection via `skills_paths`.**
All three spawners build `copy_cmd = " && ".join(f"cp -r {p} /skills-data/" ...)` and
run it via `sh -c` (`kubernetes_spawner.py:169,186`; `podman_spawner.py:105,108`;
`openshell_spawner.py:731,737`). `skills_paths` comes directly from the API request.
A value like `/skills; curl … | sh` executes in the init container — which in the K8s
path has **no `securityContext` set** (`kubernetes_spawner.py:182-194`), so it may run
as root depending on the image/PSA.
*Fix:* pass paths as `cp` argv items instead of interpolating into a shell string;
give the init container the same securityContext as the main container.

### Medium severity

**4. Credential isolation not achieved on K8s/Podman.**
R8 says "Credentials never exposed to agent processes," but the K8s spawner injects
the LLM secret as environment variables via `env_from`
(`kubernetes_spawner.py:238-244`), and `temporal_activities.py:308-311` reads the
runner's own env and passes the plaintext credential into the pod env. The agent
process can read these via `os.environ`. Only the OpenShell network-level injection
actually meets the claim. The HTML FAQ correctly scopes the strong statement to
OpenShell, but the R8 requirement card states it unconditionally — reconcile the two.

**5. NetworkPolicy guardrail claimed but missing on K8s.**
The FAQ lists "K8s NetworkPolicy … scoped egress to known services" as a guardrail.
NetworkPolicy is only built in `openshell_spawner.py:250`; `KubernetesSpawner` never
creates one. Sandbox pods therefore have unrestricted egress on K8s, which is what
makes #2 exploitable for exfiltration.

**6. Secret redaction only covers error paths.**
`redact.py` is applied solely to activity error/exception strings
(`temporal_activities.py:562-593`). Transcripts (full tool-call inputs/outputs, saved
to PostgreSQL and returned via `/transcript` and `/handoff`) and escalation packages
are never redacted. Secrets appearing in tool output would be persisted and served
unredacted.

**7. Path traversal in CLI-handoff escalation.**
`escalation.py:383-386` builds `filename = f"handoff-{wf_id}.md"` from the
workflow_id, which for API-triggered runs is caller-controlled
(`temporal_api.py:327`, `request.workflow_id or …`). A `workflow_id` like `../../…`
escapes `output_dir` when the `cli_handoff` packager is configured.
*Fix:* validate/slugify `workflow_id`.

**8. TokenReview auth does blocking I/O on the event loop.**
`auth.py:236-250` calls `config.load_incluster_config()` and the synchronous
`create_token_review()` inside an async `dispatch`, building a new client per request.
Under load this stalls the loop.
*Fix:* use the async client or a thread executor; cache the API client.

### Low severity

- **Prompt-injection nuance:** `{{ input }}` is substituted with raw user input
  *before* the templating pass and without the `<data>` wrapper other values get
  (`temporal_workflow.py:408-417`), so user input containing
  `{{ steps.X.output.Y }}` is expanded on the second pass — a within-workflow
  data-reference path that bypasses the injection mitigation in `interpolation.py`.
- **SSE endpoints** (`temporal_api.py:742-780`, `924-938`) poll every second in an
  unbounded loop with no max lifetime, holding a Temporal query per second per
  client — a slow-resource-exhaustion vector at scale.

---

## Functional perspective (against R1–R12)

Most requirements are genuinely implemented:

| Req | Status | Notes |
|-----|--------|-------|
| R1 Framework, not agents | ✅ | Generic `AgentWorkflow` interprets any YAML |
| R2 Multi-step + oversight | ✅ | Conditions, retry, approval gates, parallel groups, escalation |
| R3 Human-out-of-the-loop | ⚠️ | Escalation works; retry-adaptation claim not met (see below) |
| R4 Ephemeral-by-default | ✅ | Fresh pod/step, `finally` destroy, orphan reconciliation, approval steps don't spawn |
| R5 Stateless runner, durable state | ⚠️ | Temporal ✅, but per-process rate-limit/dedup/circuit-breaker undercut multi-replica |
| R6 Agent memory across steps | ✅ | Transcript store + context forwarding |
| R7 Cross-platform | ✅ | K8s / Podman / OpenShell spawners |
| R8 Security | ⚠️ | Credentials in env on K8s/Podman; auth + TLS opt-in; tool scoping forwarded not enforced |
| R9 Access control | ⚠️ | Fail-closed when enabled, but default `none` |
| R10 Observability | ✅ | OTel, Prometheus, structlog, audit events |
| R11 Triggers | ✅ | API, alert, schedule |
| R12 Agents-as-tools | ⛔ | Correctly marked "to be explored" — not implemented |

Two gaps worth calling out:

- **R3's "each retry sees prior failure history so the agent can adapt before
  escalating" is not implemented.** Retries go through Temporal's `RetryPolicy`
  (`temporal_workflow.py:316`), which re-invokes the activity with *identical* input.
  The prompt is built once in `_handle_agent_step` and never augmented with the
  previous attempt's error, so the agent cannot adapt between retries — it just
  re-runs. The escalation package is built only after retries exhaust.
  *Fix:* feed prior-attempt failures into the next attempt's context, or soften the
  requirement claim.
- **Per-process state undercuts R5's horizontal scaling.** The rate limiter
  (`rate_limiter.py`, admits this in its own docstring), the alert dedup tracker
  (`alert_trigger.py:197`), and the circuit breaker (module-global in
  `temporal_activities.py:40`) are all in-memory per replica. Across replicas, dedup
  and rate limits are only fractionally effective and the circuit breaker state
  diverges. Fine for single-replica; needs shared (Redis) state for the multi-replica
  story.

---

## Quality perspective

Generally high: consistent typing, docstrings, fail-closed intent in the risk
classifier (`auto_approve.py:104`) and condition evaluator, good separation of "what"
(YAML) vs "how" (API request). Specific issues:

- **Condition parser splits on `" or "` / `" and "` literally**
  (`conditions.py:37-42`), so any string value containing those substrings (e.g.
  `output.msg == "restart or reboot"`) parses wrong. It fails closed, but silently —
  the step is skipped unexpectedly. Tokenize instead of `str.split`.
- **`except (json.JSONDecodeError, Exception)`** (`temporal_activities.py:231`) is
  redundant — `Exception` already covers it; reads as a mistake.
- **Schema / field duplication and dead fields.** `WorkflowStepSpec` has both a
  top-level `service_account` (`definition.py:49`) and `permissions.service_account`;
  only the latter is read. Combined with the documented dead fields (`spawn`,
  `agent`, `spawn_config`) this is a confusing surface — Pydantic v2 silently ignores
  unknown fields, so a typo'd live field would be dropped. Consider `extra="forbid"`
  on the step model and removing dead fields.
- **File-handle leaks** in `_build_tls_config` (`temporal_entrypoint.py:59-61`) —
  `open(...).read()` without closing.
- **`{{ input }}` interpolation is exact-match only** (`temporal_workflow.py:410`);
  `{{input}}` or extra spaces silently won't substitute.

---

## Top recommendations (priority order)

1. Make security the default: `AUTH_REQUIRED=true`, and reconsider `WORKFLOW_AUTHZ`
   defaulting to `none`; or clearly document that the framework ships
   insecure-by-default.
2. Add an **image allowlist and ServiceAccount allowlist**; validate both in
   `validate_definition` / at `/run`.
3. Fix `skills_paths` shell injection (argv, not `sh -c`) and set a securityContext on
   the K8s init container.
4. Implement K8s NetworkPolicy in `KubernetesSpawner`, or drop the claim from the doc
   until it exists.
5. Extend redaction to transcripts and escalation packages, not just error strings.
6. Reconcile the doc's R3 (retry adaptation) and R8 (credential isolation) claims with
   the K8s reality, and note the per-replica limitation of
   rate-limit/dedup/circuit-breaker against R5.
