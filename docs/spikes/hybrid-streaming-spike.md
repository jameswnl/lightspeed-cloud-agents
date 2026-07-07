# Spike: Hybrid OpenShell Exec with Streaming Progress

**Issue**: #52
**Status**: Spike complete (Tasks 2-4 implemented)

## Architecture

```
Workflow Runner (Temporal Activity)
  |
  +-- spawner.spawn(pod_name, image, env)
  |     |
  |     +-- OpenShellSpawner._do_spawn()
  |           |
  |           +-- client.create_sandbox(image, env, labels)
  |           +-- start_server(sandbox_id, ["uvicorn", ...])   # fire-and-forget
  |           +-- client.expose_service(sandbox_id, port=8080) # routable URL
  |
  +-- spawner.wait_ready(endpoint, "/health")
  |
  +-- [OpenShell only] asyncio.create_task(progress_streaming_loop)
  |     |
  |     +-- spawner.stream_progress(sandbox_id)
  |     |     +-- exec_stream(["tail", "-f", "/var/log/agent-events.jsonl"])
  |     |     +-- parse JSONL -> yield event dicts
  |     |
  |     +-- _truncate_heartbeat_payload(event)   # <1KB summary
  |     +-- activity.heartbeat({"event_type": "tool_call", "tool": "get_pods"})
  |
  +-- POST /v1/agent/run (HTTP result -- source of truth)
  |
  +-- Cancel progress task
  +-- spawner.destroy(pod_name)
```

## Cross-Spawner Degradation Strategy

Progress streaming is **OpenShell-specific**. It is NOT part of the `AgentSpawner` ABC.

| Spawner        | HTTP Result | Progress Streaming |
|----------------|-------------|-------------------|
| OpenShellSpawner | Yes       | Yes (best-effort) |
| KubernetesSpawner | Yes     | No                |
| PodmanSpawner    | Yes       | No                |

The wiring in `temporal_activities.py` uses `isinstance(spawner, OpenShellSpawner)` to check
before starting progress streaming. Non-OpenShell spawners get the existing behavior unchanged
(periodic heartbeats only, no progress events).

## Heartbeat Payload Strategy

Temporal heartbeat payloads have size limits. We use a truncation strategy:

- **What we heartbeat**: Event type + tool name only (`{"event_type": "tool_call", "tool": "get_pods"}`)
- **Max size**: <1KB per heartbeat payload
- **What we drop**: Full event input/output, thinking content, timestamps
- **Future**: If full event history is needed, store in a separate mechanism (e.g., workflow query or external store)

The `_truncate_heartbeat_payload()` function extracts only essential fields:

```python
{"event_type": "tool_call", "tool": "get_pods"}  # ~50 bytes
```

## SSE Event Types

New event type added to the SSE stream:

- `step.progress` -- forwarded from heartbeat data via workflow events

These integrate with the existing `WorkflowEvent` model and SSE cursor-based streaming. No
changes to the SSE endpoint logic were needed; the existing generic event streaming handles
`step.progress` events naturally.

In production, the workflow would emit `step.progress` events into its event list when it
receives heartbeat data containing progress payloads. The workflow already has a
`heartbeat_timeout` of 180s, providing automatic detection of stale workers.

## Task 1 (Sandbox Side) -- Follow-up Required

**Repo**: lightspeed-agentic-sandbox

The sandbox agent needs to write structured JSONL events to `/var/log/agent-events.jsonl`:

```jsonl
{"type": "tool_call", "name": "get_pods", "input": "...", "ts": "2024-01-01T00:00:00Z"}
{"type": "tool_result", "name": "get_pods", "output": "...", "ts": "2024-01-01T00:00:01Z"}
{"type": "thinking", "content": "Let me analyze...", "ts": "2024-01-01T00:00:02Z"}
{"type": "result", "output": "...", "ts": "2024-01-01T00:00:03Z"}
```

The existing `EventLogger` in the sandbox already captures these events on stderr. Adding a
file sink alongside the stderr logger is straightforward.

**Key requirement**: The file must flush line-by-line (not buffered) so `tail -f` picks up
events in real-time. Use line-buffered mode or explicit `flush()` after each write.

## Implementation Summary

### Files Changed

| File | Change |
|------|--------|
| `src/cloud_agents/spawner/openshell_spawner.py` | New: OpenShellSpawner with start_server(), stream_progress() |
| `src/cloud_agents/workflow/temporal_activities.py` | Added: progress streaming loop, heartbeat truncation, isinstance wiring |
| `tests/unit/spawner/test_openshell_spawner.py` | New: 14 tests for OpenShellSpawner |
| `tests/unit/workflow/temporal/test_progress_streaming.py` | New: 11 tests for progress streaming |
| `tests/unit/workflow/temporal/test_sse_progress.py` | New: 4 tests for SSE event types |

### Test Coverage

- 14 tests for OpenShellSpawner (start_server, stream_progress, spawn, destroy, list_active)
- 11 tests for progress streaming wiring (OpenShell detection, truncation, cancellation, degradation)
- 4 tests for SSE step.progress events
- Full existing test suite (853 tests) passes with no regressions

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| ExecSandbox for server startup may behave differently than entrypoint | start_server is fire-and-forget; HTTP readiness probe validates startup |
| Two concurrent ExecSandbox calls to same sandbox | stream_progress uses separate exec call; verify OpenShell supports this |
| Event file may buffer (no real-time flush) | Sandbox must use line-buffered writes; document as requirement for Task 1 |
| Temporal heartbeat size limits | Truncation strategy keeps payloads <1KB |
| Progress stream disconnects mid-run | Best-effort; errors logged and ignored; HTTP result is source of truth |

## Go/No-Go Recommendation

**GO** -- The hybrid approach is sound:

1. HTTP contract preserved as source of truth -- zero risk of data loss from streaming issues
2. Progress streaming is purely additive and best-effort -- failures cannot affect results
3. Clean cross-spawner degradation via isinstance check -- no ABC pollution
4. Heartbeat truncation keeps payloads well within Temporal limits
5. All 29 new tests pass; no regressions in existing 853 tests
6. Task 1 (sandbox side) is a straightforward file sink addition

**Next steps**: Implement Task 1 in lightspeed-agentic-sandbox, then integration test the
full pipeline end-to-end.
