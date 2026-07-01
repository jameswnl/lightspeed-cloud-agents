# Phase 2 — Implementation Plan

**Tasks**: T7, T17, T19, T21, T24
**Focus**: Production hardening
**T7 design**: `t7-rbac-design.md` (LGTM after 3 review rounds)

## Execution Order

```
Batch A (quick wins, ~2 days):
  T17 (alerting rules)  ─── independent (1 day)
  T19 (circuit breaker) ─── independent (1-2 days)
  T21 (interpolation)   ─── independent (half day)
  T24 (PDB)             ─── independent (half day)

Batch B (design-heavy, ~5 days):
  T7 (RBAC)             ─── after Batch A verified
```

Batch A tasks are all independent and can run in parallel.

---

## T17: Prometheus alerting rules

### What changes

Add a PrometheusRule manifest with alerts for common failure modes.

### Code changes

**`deploy/helm/templates/prometheusrule.yaml`** (new):
```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: {{ .Release.Name }}-alerts
spec:
  groups:
    - name: cloud-agents
      rules:
        - alert: WorkflowStepFailureRateHigh
          expr: rate(ls_workflow_step_runs_total{status="failed"}[5m]) > 0.1
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "Workflow step failure rate above 10%"

        - alert: SandboxCleanupFailure
          expr: increase(ls_sandbox_cleanup_failures_total[10m]) > 0
          for: 1m
          labels:
            severity: warning
          annotations:
            summary: "Sandbox container cleanup failed — possible container leak"

        - alert: SandboxOrphanDetected
          expr: increase(ls_sandbox_orphans_cleaned_total[10m]) > 0
          for: 1m
          labels:
            severity: warning
          annotations:
            summary: "Orphaned sandbox containers cleaned up on startup — possible prior crash"

        - alert: WorkflowRunnerNotReady
          expr: probe_success{job="workflow-runner", path="/readyz"} == 0
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: "Workflow runner readyz probe failed — Temporal connectivity lost"

        - alert: WorkflowRunnerDown
          expr: up{job="workflow-runner"} == 0
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: "Workflow runner is down"
```

**Alert coverage** (covers all currently available metrics):
- Step failure rate → `WorkflowStepFailureRateHigh`
- Sandbox cleanup failures → `SandboxCleanupFailure`
- Orphaned pods → `SandboxOrphanDetected`
- Temporal worker health → `WorkflowRunnerNotReady` (readyz probe = Temporal connectivity)
- Runner down → `WorkflowRunnerDown`

**Cross-task dependency**: The parent roadmap's LLM provider error alert is completed jointly by T17 + T19. T19 adds circuit breaker metrics (`CircuitBreakerOpen`); T17 adds the corresponding alert rule after T19 lands. Neither task alone closes the parent scope.

**`deploy/helm/values.yaml`** — add:
```yaml
prometheusRule:
  enabled: false
```

### Tests

```
TestPrometheusRule:

1. test_helm_template_renders_prometheusrule
   - helm template with prometheusRule.enabled=true
   - Assert PrometheusRule YAML is valid
   - Assert alert names present

2. test_helm_template_no_prometheusrule_by_default
   - helm template with defaults
   - Assert no PrometheusRule rendered
```

These are Helm template tests (shell-based), not Python unit tests.

### Effort: 1 day

---

## T19: Circuit breaker for LLM provider

### What changes

Track recent sandbox step failures. After N consecutive failures in M seconds, fail fast instead of spawning more sandboxes that will time out.

### Code changes

**`src/cloud_agents/workflow/circuit_breaker.py`** (new):
```python
class ProviderCircuitBreaker:
    """Per-provider circuit breaker. Tracks consecutive failures keyed by provider name.

    Scope: per-process only. In multi-replica deployments, each runner
    tracks its own breaker state independently. This is acceptable as
    an interim step — shared state (Redis) is deferred.
    """

    def __init__(self, failure_threshold: int = 5, reset_seconds: float = 60.0):
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._providers: dict[str, _ProviderState] = {}

    def record_success(self, provider: str) -> None:
        if provider in self._providers:
            self._providers[provider].failures = 0

    def record_failure(self, provider: str) -> None:
        state = self._providers.setdefault(provider, _ProviderState())
        state.failures += 1
        state.last_failure = time.monotonic()

    def is_open(self, provider: str) -> bool:
        state = self._providers.get(provider)
        if state is None or state.failures < self._threshold:
            return False
        elapsed = time.monotonic() - state.last_failure
        if elapsed > self._reset_seconds:
            state.failures = 0
            return False
        return True

class _ProviderState:
    def __init__(self):
        self.failures = 0
        self.last_failure = 0.0
```

**Scope limitation**: Per-process, per-provider. In multi-replica deployments, one runner can open its breaker while others keep spawning. This is acceptable for Phase 2. Shared breaker state (Redis) is a future enhancement.

**`src/cloud_agents/workflow/temporal_activities.py`** — check circuit breaker keyed by provider before spawning:
```python
provider_name = provider.get("name", "unknown")
if circuit_breaker.is_open(provider_name):
    return {"status": "failed", "error": f"Circuit breaker open for provider '{provider_name}'"}
```
On success: `circuit_breaker.record_success(provider_name)`. On failure: `circuit_breaker.record_failure(provider_name)`.

**Configuration**: `CIRCUIT_BREAKER_THRESHOLD` (default 5), `CIRCUIT_BREAKER_RESET_SECONDS` (default 60).

### Tests

```
TestProviderCircuitBreaker:

1. test_closed_by_default
   - New breaker → is_open("openai") returns False

2. test_opens_after_threshold_failures
   - Record 5 failures for "openai" → is_open("openai") returns True

3. test_resets_after_timeout
   - Record 5 failures, wait > reset_seconds → is_open("openai") returns False

4. test_success_resets_counter
   - Record 3 failures for "openai", then 1 success → is_open("openai") returns False

5. test_below_threshold_stays_closed
   - Record 4 failures (threshold=5) → is_open("openai") returns False

6. test_providers_isolated
   - Record 5 failures for "openai" → is_open("openai") True
   - is_open("gemini") still False (different provider)

7. test_one_provider_failure_does_not_affect_another
   - Record failures for "openai", success for "gemini"
   - Assert each provider tracked independently

TestCircuitBreakerInActivity:

8. test_open_breaker_returns_failed_without_spawning
   - Mock circuit_breaker.is_open("openai") → True
   - Call run_sandbox_step with provider name "openai"
   - Assert spawner.spawn NOT called
   - Assert result status == "failed"
   - Assert error message includes provider name

9. test_success_records_on_breaker
   - Successful sandbox step → circuit_breaker.record_success("openai") called

10. test_failure_records_on_breaker
    - Failed sandbox step → circuit_breaker.record_failure("openai") called
```

### Effort: 1-2 days

---

## T21: Template interpolation sanitization

### What changes

Prevent recursive template injection and limit interpolated value size.

### Code changes

**`src/cloud_agents/workflow/interpolation.py`** — add validation:
```python
MAX_INTERPOLATED_VALUE_LENGTH = 10000

def interpolate(template: str, state: WorkflowState) -> str:
    # ... existing interpolation ...
    for ref, value in replacements.items():
        value_str = str(value)
        if len(value_str) > MAX_INTERPOLATED_VALUE_LENGTH:
            logger.warning("Interpolated value for '%s' truncated (%d chars)", ref, len(value_str))
            value_str = value_str[:MAX_INTERPOLATED_VALUE_LENGTH] + "..."
        if "{{" in value_str:
            logger.warning("Interpolated value for '%s' contains template syntax — not expanded", ref)
        result = result.replace(ref, value_str)
    return result
```

### Tests

```
TestInterpolationSanitization:

1. test_recursive_template_not_expanded
   - Step output contains "{{ steps.other.output.x }}"
   - Interpolated into prompt → literal string, NOT recursively expanded

2. test_large_value_truncated
   - Step output is 20000 chars
   - Interpolated value truncated to MAX_INTERPOLATED_VALUE_LENGTH + "..."

3. test_normal_value_not_truncated
   - Step output is 100 chars → passes through unchanged

4. test_template_syntax_warning_logged
   - Step output contains "{{"
   - Assert warning logged
```

### Effort: Half day

---

## T24: Pod disruption budgets

### What changes

Add PDB template to Helm chart.

### Code changes

**`deploy/helm/templates/pdb.yaml`** (new):
```yaml
{{- if and .Values.podDisruptionBudget.enabled (gt (int .Values.workflowRunner.replicas) 1) }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .Release.Name }}-workflow-runner
spec:
  minAvailable: {{ .Values.podDisruptionBudget.minAvailable }}
  selector:
    matchLabels:
      app: workflow-runner
{{- end }}
```

**`deploy/helm/values.yaml`** — add:
```yaml
podDisruptionBudget:
  enabled: true
  minAvailable: 1
```

### Tests

```
TestPDB:

1. test_pdb_rendered_when_replicas_gt_1
   - helm template with replicas=2, pdb.enabled=true
   - Assert PDB YAML valid with minAvailable=1

2. test_pdb_not_rendered_with_single_replica
   - helm template with replicas=1, pdb.enabled=true
   - Assert no PDB (pointless with 1 replica)

3. test_pdb_not_rendered_when_disabled
   - helm template with pdb.enabled=false
   - Assert no PDB
```

### Effort: Half day

---

## T7: Per-user/team RBAC

See `t7-rbac-design.md` for full design (LGTM'd).

### Implementation steps (from design)

| Step | What | Effort |
|---|---|---|
| 1 | Define models: `CallerIdentity`, `WorkflowAction`, `WorkflowResource`, `WorkflowAuthzContext`, `AuthzDecision`, `ApproverInfo` | 2 hours |
| 2 | Implement `CallerIdentity` extraction — update auth middleware, add `get_caller_identity` dependency | Half day |
| 3 | Implement `WorkflowAuthorizer` ABC + `NoopAuthorizer` | 1 hour |
| 4 | Implement `PolicyFileAuthorizer` with YAML loading + rule matching | 1 day |
| 5 | Wire authorizer into `build_temporal_router` — all endpoints | 1 day |
| 6 | Add `WorkflowAuthzContext` to `WorkflowInput` — capture at trigger, persist, expose | Half day |
| 7 | Add `ApproverInfo` to approval signal + workflow state | Half day |
| 8 | Tests | 1.5 days |

### Tests (from design — 20 tests)

See `t7-rbac-design.md` Tests section.

### Effort: ~5 days

---

## Verification Checklist

- [ ] T17: `helm template` with `prometheusRule.enabled=true` renders valid PrometheusRule
- [ ] T19: Circuit breaker opens after threshold failures, resets after timeout
- [ ] T19: Open breaker prevents sandbox spawning
- [ ] T21: Template `{{` in interpolated value not recursively expanded
- [ ] T21: Large interpolated values truncated
- [ ] T24: PDB rendered when replicas > 1, not rendered when 1
- [ ] T7: Unauthorized trigger returns 403
- [ ] T7: Authorized trigger succeeds
- [ ] T7: Approver identity recorded in workflow state
- [ ] T7: Policy file rules enforce view/trigger/approve permissions
- [ ] T7: Fail-closed: authz enabled + no identity → 401
- [ ] T7: Unauthorized GET /definitions returns 403
- [ ] T7: Unauthorized POST /definitions returns 403
- [ ] T7: Authorized GET /definitions succeeds with VIEW_DEFS permission
- [ ] All existing tests still pass (322 baseline)
