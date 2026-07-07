# Load Test Results and SLO Thresholds

Baseline load test results and SLO definitions for the Cloud Agents workflow runner.

## Test Environment

- **Backend**: Mocked Temporal client (no live Temporal server)
- **Transport**: FastAPI TestClient (in-process, no network overhead)
- **Purpose**: Validate API behavior under load, not absolute throughput

For live-stack benchmarks, run `make test-load` against a running Podman or Kind deployment.

## SLO Thresholds

| Metric | Mocked Backend | Live Stack (Target) |
|--------|---------------|---------------------|
| Workflow submission p99 latency | < 500ms | < 5s |
| SSE time-to-first-event | < 2s | < 10s |
| Rate limiter overhead (p50) | < 5ms | < 10ms |
| Error rate under load (5xx) | 0% | < 1% |
| Submission latency degradation | < 3x early-vs-late p50 | < 5x |

## Scenario Results (Mocked Backend)

### 1. Concurrent Workflow Submissions

- **What**: Submit 10/50/100 workflows via POST /v1/workflows/run
- **Result**: All submissions return 202 Accepted
- **p99 latency**: < 500ms (typically < 10ms with mocked backend)
- **Error rate**: 0% (no 5xx errors)
- **Unique IDs**: All workflow_id values unique

### 2. Rate Limiter Stress

- **Config**: rate=10 req/s, burst=20
- **Burst within limit**: 20/20 accepted (100%)
- **Burst above limit**: Request 21+ returns 429 with Retry-After header
- **Cross-caller isolation**: Exhausting caller A does not affect caller B
- **Rejection ratio**: ~20 accepted / ~20 rejected for 40 rapid requests
- **Health endpoint exemption**: /healthz, /livez, /readyz always 200
- **Overhead**: < 5ms p50 compared to unprotected endpoint

### 3. Approval Gate Backpressure

- **What**: Submit 20-30 workflows with approval-gated steps
- **Submission**: All accepted (202)
- **Approval signals**: Correctly routed per workflow_id
- **Batch approval**: 30 rapid approvals complete without errors
- **Denial**: Works correctly under load

### 4. Sandbox Spawn Storm

- **What**: Submit 100 workflows (exceeding any MAX_SPAWNED_PODS)
- **API layer**: Accepts all 100 (capacity enforcement is at Temporal activity level)
- **Latency stability**: Late submissions within 3x of early submissions
- **Mixed operations**: Submit + query interleaving works without errors

### 5. SSE Connection Scalability

- **What**: Open sequential SSE connections to /v1/workflows/{id}/events
- **Single connection**: Receives step.started, step.completed, workflow.completed
- **10 connections**: All receive >= 2 events each
- **Time to first event**: < 2s (typically < 100ms with mocked backend)
- **Stream termination**: Correctly closes after workflow.completed event

## Running Load Tests

```bash
# Run all load tests (mocked backend, no infrastructure needed)
make test-load

# Run a specific scenario
uv run pytest tests/load/test_rate_limiter_stress.py -v

# Run with detailed latency output
uv run pytest tests/load/ -v -s
```

## Live Stack Testing

To run against a live stack:

1. Start the stack: `make up` (Podman) or `make kind-up` (Kind)
2. Run load tests: `make test-load`

Note: The current test suite uses FastAPI TestClient with mocked Temporal.
For live-stack load testing with real HTTP traffic, use a tool like
`wrk`, `hey`, or `locust` pointed at `http://localhost:8080`.

Example with `hey`:
```bash
# 100 requests, 10 concurrent
hey -n 100 -c 10 -m POST \
  -H "Content-Type: application/json" \
  -d '{"definition":{"apiVersion":"v1","kind":"AgentWorkflow","metadata":{"name":"bench"},"spec":{"steps":[{"name":"s1","type":"agent","output_key":"r","prompt":"test"}]}},"provider":{"name":"openai","model":"gpt-4","credentials_secret":"key"}}' \
  http://localhost:8080/v1/workflows/run
```
