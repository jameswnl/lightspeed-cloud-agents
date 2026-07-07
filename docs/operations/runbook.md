# Cloud Agents Operational Runbook

Procedures for diagnosing and recovering from common failure scenarios. Every section links to the relevant metric, endpoint, or source file so you can verify the procedure against the running system.

---

## Prerequisites

- Access to the workflow runner pod/container logs
- Access to Prometheus metrics at the `/metrics` endpoint
- `kubectl` (Kubernetes) or `podman` (Podman) CLI access
- Familiarity with [DEPLOYMENT.md](../DEPLOYMENT.md) for configuration reference

---

## 1. Health Check Failures

The workflow runner exposes three probe endpoints (defined in `src/cloud_agents/workflow/temporal_entrypoint.py`):

| Endpoint | Purpose | Healthy response |
|----------|---------|------------------|
| `/healthz` | Basic liveness | `{"status": "ok"}` (200) |
| `/livez` | K8s liveness probe | `{"status": "alive"}` (200) |
| `/readyz` | Readiness -- Temporal connectivity | `{"status": "ready"}` (200) or `{"status": "not_ready"}` (503) |

### Symptom: `/readyz` returns 503

The runner process is alive but cannot reach Temporal.

**Diagnosis**:
```bash
# Check readiness
curl -s http://localhost:8080/readyz

# Check runner logs for Temporal connection errors
kubectl logs deploy/workflow-runner | grep -i "cannot connect to temporal"
# or for Podman:
podman logs workflow-runner 2>&1 | grep -i "cannot connect to temporal"
```

**Recovery**:
1. Verify Temporal server is running: `kubectl get pods -l app=temporal`
2. Check the `TEMPORAL_URL` env var points to the correct address (default: `localhost:7233`)
3. Check the `TEMPORAL_NAMESPACE` env var (default: `default`)
4. If TLS is enabled (`TEMPORAL_TLS_ENABLED=true`), verify cert paths:
   - `TEMPORAL_TLS_CERT_PATH`
   - `TEMPORAL_TLS_KEY_PATH`
   - `TEMPORAL_TLS_CA_PATH`
5. Restart the runner -- it will retry the Temporal connection on startup

### Symptom: `/healthz` not responding

The runner process has crashed or is not accepting connections.

**Diagnosis**:
```bash
kubectl describe pod -l app=workflow-runner
kubectl logs deploy/workflow-runner --previous
```

**Recovery**:
1. Check for OOM kills in pod events
2. Check `MAX_REQUEST_BODY_BYTES` -- a very large value may allow memory exhaustion
3. Restart the pod: `kubectl rollout restart deploy/workflow-runner`

---

## 2. Orphaned Sandbox Containers

On startup the runner scans for containers labelled `spawned-by=workflow-runner` and destroys them (see `reconcile_orphaned_sandboxes()` in `src/cloud_agents/workflow/temporal_entrypoint.py`). If this cleanup fails or sandboxes accumulate between restarts, you have orphans.

**Metrics**:
- `ls_sandbox_orphans_cleaned_total` -- incremented when orphans are destroyed on startup
- `ls_sandbox_cleanup_failures_total` -- incremented when `spawner.destroy()` fails

**Alerts** (from `deploy/helm/templates/prometheusrule.yaml`):
- `SandboxOrphanDetected` -- fires when `ls_sandbox_orphans_cleaned_total` increases
- `SandboxCleanupFailure` -- fires when `ls_sandbox_cleanup_failures_total` increases

### Symptom: `SandboxOrphanDetected` alert fires

Orphans were found and cleaned on runner startup. This usually means a previous runner crash.

**Diagnosis**:
```bash
# Check audit log for orphan cleanup details
kubectl logs deploy/workflow-runner | grep "orphan_cleanup"

# Check metric value
curl -s http://localhost:8080/metrics | grep ls_sandbox_orphans_cleaned_total
```

**Recovery**:
1. Review previous runner crash cause in logs
2. Orphans were already cleaned automatically -- no manual action needed
3. If orphans persist after restart, run manual cleanup:

```bash
# Kubernetes
kubectl delete pods -l spawned-by=workflow-runner

# Podman
make clean-sandboxes
```

### Symptom: `SandboxCleanupFailure` alert fires

The runner tried to destroy a sandbox but `spawner.destroy()` raised an exception.

**Diagnosis**:
```bash
kubectl logs deploy/workflow-runner | grep "Failed to destroy"
curl -s http://localhost:8080/metrics | grep ls_sandbox_cleanup_failures_total
```

**Recovery**:
1. Identify the stuck container from logs
2. Delete it manually:
```bash
# Kubernetes
kubectl delete pod <sandbox-pod-name> --force --grace-period=0

# Podman
podman rm -f <sandbox-container-name>
```
3. Check if the spawner has permissions to delete pods (`SPAWNER_NAMESPACE`, `SPAWNER_SERVICE_ACCOUNT`)

---

## 3. Workflow Stuck or Hung

A workflow may appear stuck in a running state without progress.

**Metrics**:
- `ls_workflow_run_duration_seconds` -- histogram; check for outliers at p99
- `ls_sandbox_timeout_total` -- incremented when a sandbox activity times out

### Symptom: Workflow remains in "running" state past expected duration

**Diagnosis**:
```bash
# Check workflow status via API
curl -s http://localhost:8080/v1/workflows/<workflow_id> | python3 -m json.tool

# Check if stuck at an approval gate
curl -s http://localhost:8080/v1/workflows/<workflow_id> | grep -i "pending_approval"

# Check Temporal UI for workflow details (if deployed)
# Default Temporal UI: http://localhost:8233
```

**Recovery options**:

1. **Stuck at approval gate**: Approve or deny via API:
```bash
# Approve
curl -s -X POST http://localhost:8080/v1/workflows/<workflow_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"step_name": "<step>", "decision": "approved"}'

# Deny
curl -s -X POST http://localhost:8080/v1/workflows/<workflow_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"step_name": "<step>", "decision": "denied"}'
```

2. **Cancel the workflow**:
```bash
curl -s -X POST http://localhost:8080/v1/workflows/<workflow_id>/cancel
```

3. **Check heartbeat timeout**: The sandbox activity heartbeats every 30 seconds with a 180-second timeout. If the sandbox is alive but the network is partitioned, Temporal will cancel the activity after 180 seconds of missed heartbeats.

4. **Check Temporal directly** (if you have `tctl` or Temporal UI):
```bash
tctl workflow describe -w <workflow_id>
tctl workflow cancel -w <workflow_id>
```

---

## 4. LLM Provider Errors

The circuit breaker (`src/cloud_agents/workflow/circuit_breaker.py`) tracks consecutive failures per LLM provider. After `CIRCUIT_BREAKER_THRESHOLD` (default: 5) consecutive failures, it opens and fails fast for `CIRCUIT_BREAKER_RESET_SECONDS` (default: 60).

**Metrics**:
- `ls_workflow_step_runs_total{status="failed"}` -- step failures (all causes)
- `ls_workflow_step_runs_total{status="success"}` -- successful steps

**Alerts**:
- `WorkflowStepFailureRateHigh` -- fires when failure rate exceeds 10% over 5 minutes

### Symptom: `WorkflowStepFailureRateHigh` alert fires

**Diagnosis**:
```bash
# Check failure rate in Prometheus
curl -s http://localhost:8080/metrics | grep ls_workflow_step_runs_total

# Check runner logs for provider errors
kubectl logs deploy/workflow-runner | grep -E "circuit breaker|provider.*fail|status_code"

# Check if circuit breaker is open
kubectl logs deploy/workflow-runner | grep "circuit breaker is open"
```

**Recovery**:
1. **Credential rotation**: Verify the LLM API key is valid. The key name is configured via `credentials_secret` in the workflow definition. Rotate it via the corresponding env var or K8s Secret.
2. **Rate limiting by provider**: Check if you hit the provider's rate limit. Reduce `RATE_LIMIT_RATE` or add delays between workflow submissions.
3. **Circuit breaker reset**: The breaker auto-resets after `CIRCUIT_BREAKER_RESET_SECONDS`. To force reset, restart the runner pod (the breaker is per-process, not persisted).
4. **Provider outage**: Check the LLM provider status page. No action possible until provider recovers.

---

## 5. Sandbox Spawn Failures

The spawner creates sandbox containers for each agent step. Failures are typically infrastructure-related.

**Metrics**:
- `ls_sandbox_cleanup_failures_total` -- cleanup failures (may indicate spawner issues)
- `ls_sandbox_timeout_total` -- timeouts during sandbox execution

### Common causes

#### Image pull failure
```bash
# Kubernetes -- check pod events
kubectl describe pod -l spawned-by=workflow-runner | grep -A5 "Events:"

# Podman -- check image availability
podman images | grep sandbox
```

**Recovery**: Ensure the sandbox image is available. Rebuild if needed:
```bash
make build-sandbox
```

For Kubernetes, verify the image is in the cluster's registry or loaded into Kind:
```bash
kind load docker-image lightspeed-agentic-sandbox:latest
```

#### Resource limits
```bash
# Check if pods are pending due to resource pressure
kubectl get pods -l spawned-by=workflow-runner -o wide
kubectl describe pod <pending-pod> | grep -A10 "Conditions:"
```

**Recovery**:
1. Check namespace resource quotas
2. Clean up completed/failed sandbox pods
3. Increase node resources or add nodes

#### Spawner misconfiguration
```bash
# Check spawner type
kubectl logs deploy/workflow-runner | grep -i "spawner"
```

Relevant env vars:
- `WORKFLOW_SPAWNER` -- `kubernetes`, `podman`, or empty (stub mode)
- `SPAWNER_NAMESPACE` -- K8s namespace for sandbox pods (default: `default`)
- `SPAWNER_SERVICE_ACCOUNT` -- SA for sandbox pods (default: `workflow-runner`)
- `SPAWNER_NETWORK` -- Podman network name (default: `cloud-agents`)

---

## 6. Rate Limiting Issues

Per-caller rate limiting is controlled by the `RateLimitMiddleware` (in `src/cloud_agents/workflow/rate_limiter.py`).

**Metrics**:
- `ls_rate_limit_rejections_total` -- total rejected requests, labelled by `path`

### Symptom: Legitimate requests getting 429 responses

**Diagnosis**:
```bash
curl -s http://localhost:8080/metrics | grep ls_rate_limit_rejections_total
kubectl logs deploy/workflow-runner | grep "rate_limit_exceeded"
```

**Recovery -- tune rate limiter**:

| Env Var | Default | Description |
|---------|---------|-------------|
| `RATE_LIMIT_ENABLED` | `false` | Enable/disable rate limiting |
| `RATE_LIMIT_RATE` | `10.0` | Requests per second per caller |
| `RATE_LIMIT_BURST` | `20` | Max burst size |

1. Increase `RATE_LIMIT_RATE` and/or `RATE_LIMIT_BURST` to accommodate legitimate load
2. To disable temporarily: set `RATE_LIMIT_ENABLED=false` and restart
3. Health endpoints (`/healthz`, `/livez`, `/readyz`, `/metrics`) are exempt from rate limiting

---

## 7. TLS Errors

When `SANDBOX_TLS_MODE=app`, the runner generates ephemeral CA and per-sandbox server certificates (see `src/cloud_agents/workflow/tls.py`). Certs are valid for 10 minutes.

**Metrics**:
- `ls_sandbox_tls_errors_total` -- TLS errors during sandbox communication, labelled by `step_name` and `error_type`

### Symptom: TLS handshake failures

**Diagnosis**:
```bash
curl -s http://localhost:8080/metrics | grep ls_sandbox_tls_errors_total
kubectl logs deploy/workflow-runner | grep -i "tls\|ssl\|certificate"
```

**Recovery**:
1. **Cert generation failure**: Check that the `cryptography` package is installed. It is an optional dependency:
   ```bash
   pip install 'lightspeed-cloud-agents[tls]'
   ```
2. **Expired certs**: Certs are valid for 10 minutes. If a sandbox step takes longer than 10 minutes, the cert may expire. Increase `timeout_seconds` in the workflow step or investigate why the step is slow.
3. **Wrong TLS mode**: Verify `SANDBOX_TLS_MODE`:
   - `disabled` -- no TLS (default)
   - `app` -- app-level ephemeral certs
   - `mesh` -- skip app-level TLS, assume service mesh handles it
4. **Service mesh conflict**: If running in a mesh (Istio), set `SANDBOX_TLS_MODE=mesh` to avoid double-encryption.
5. **Restart**: TLS state is per-process. Restarting the runner regenerates the ephemeral CA.

---

## 8. Alert and Schedule Trigger Issues

### Alert trigger (`/v1/webhooks/alertmanager`)

Enabled via `ALERT_TRIGGER_ENABLED=true`. Maps Alertmanager alerts to workflows.

**Metrics**:
- `ls_alert_triggers_total` -- trigger outcomes, labelled by `workflow_name` and `status`

**Common issues**:

| Problem | Cause | Fix |
|---------|-------|-----|
| Alert not triggering workflow | Missing label mapping | Ensure Prometheus alert has the `ALERT_TRIGGER_WORKFLOW_LABEL` label (default: `cloud_agents_workflow`) |
| Duplicate workflows | Dedup window too short | Increase `ALERT_TRIGGER_DEDUP_WINDOW` (default: 300 seconds) |
| Alert rejected by RBAC | Namespace mismatch | Check `ALERT_TRIGGER_NAMESPACE` matches the authorized namespace |
| No default workflow | Unmapped alert, no fallback | Set `ALERT_TRIGGER_DEFAULT_WORKFLOW` |

### Schedule trigger (`/v1/schedules`)

Enabled via `SCHEDULE_TRIGGER_ENABLED=true`. Uses Temporal's native Schedule API.

**Metrics**:
- `ls_schedule_triggers_total` -- schedule trigger outcomes

**Diagnosis**:
```bash
# List schedules
curl -s http://localhost:8080/v1/schedules

# Check schedule status
curl -s http://localhost:8080/v1/schedules/<schedule_id>

# Check logs for schedule events
kubectl logs deploy/workflow-runner | grep "schedule_"
```

**Recovery**:
- Pause a misfiring schedule: `curl -s -X POST http://localhost:8080/v1/schedules/<id>/pause`
- Resume: `curl -s -X POST http://localhost:8080/v1/schedules/<id>/resume`
- Delete: `curl -s -X DELETE http://localhost:8080/v1/schedules/<id>`

---

## 9. General Diagnostics

### Collecting diagnostic data

```bash
# Full metrics dump
curl -s http://localhost:8080/metrics > metrics-$(date +%s).txt

# Runner logs (last 1000 lines)
kubectl logs deploy/workflow-runner --tail=1000 > runner-logs-$(date +%s).txt

# Audit events only
kubectl logs deploy/workflow-runner | grep '"event_type"' > audit-$(date +%s).json

# Active sandboxes
kubectl get pods -l spawned-by=workflow-runner -o wide
# or for Podman:
podman ps --filter label=spawned-by=workflow-runner
```

### Key log patterns to search for

| Pattern | Meaning |
|---------|---------|
| `"event_type": "orphan_cleanup"` | Orphans cleaned on startup |
| `"event_type": "sandbox_timeout"` | Sandbox activity timed out |
| `"event_type": "auth_rejected"` | Authentication failure |
| `"event_type": "rate_limit_exceeded"` | Request rate-limited |
| `"event_type": "tls_error"` | TLS communication error |
| `"event_type": "content_policy_violation"` | Workflow definition rejected by policy |
| `circuit breaker is open` | Provider circuit breaker tripped |
| `Cannot connect to Temporal` | Temporal server unreachable |

### Runner restart checklist

Before restarting the workflow runner:
1. In-flight workflows will be re-dispatched by Temporal after heartbeat timeout (180s)
2. Orphaned sandbox containers will be cleaned on the next startup
3. Circuit breaker state resets (per-process)
4. Rate limiter state resets (per-process)
5. Any in-memory state (definition store cache) will be lost

### Alert reference

All alerts are defined in `deploy/helm/templates/prometheusrule.yaml`:

| Alert | Condition | Severity |
|-------|-----------|----------|
| `WorkflowStepFailureRateHigh` | Step failure rate > 10% for 5 min | warning |
| `SandboxCleanupFailure` | Any cleanup failure in 10 min | warning |
| `SandboxOrphanDetected` | Any orphan cleaned in 10 min | warning |
| `WorkflowRunnerDown` | Runner target down for 2 min | critical |
