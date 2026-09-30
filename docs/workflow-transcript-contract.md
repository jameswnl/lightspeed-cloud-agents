# Workflow Transcript Contract

Canonical per-step transcript event contract for agent workflow steps,
identical across all spawn modes (`none`, `local`, `ephemeral`).

The event shape and data keys are defined by the sandbox agent's
`EventLogger` (`lightspeed-agentic-sandbox` `src/lightspeed_agentic/logging.py`)
— the ephemeral mode's producer. Spawn modes `none` (`DirectExecutor`) and
`local` (`SubprocessExecutor`) reconstruct the same events from the
pydantic-ai message history via
`cloud_agents.workflow.executor.step.transcript_events`.

## Event shape

Every event is a JSON object with exactly three keys:

```json
{"ts": "2026-09-30T00:00:00+00:00", "type": "tool_call", "data": {}}
```

| `type` | `data` keys | Produced when |
|---|---|---|
| `thinking` | `text` | Model reasoning block (truncated at 2000 chars) |
| `tool_call` | `name`, `input` | Agent requests a tool; `input` is the JSON-stringified arguments (truncated at 2000 chars) |
| `tool_result` | `output` | Tool return value, stringified (truncated at 2000 chars) |
| `result` | `text`, `cost_usd`, `input_tokens`, `output_tokens` | Final output of a run |
| `error` | `message` | Run failed |

Event order is significant (it mirrors execution order); `ts` ordering is
not relied upon.

## Per-spawn-mode behavior

| | `none` (DirectExecutor) | `local` (SubprocessExecutor) | `ephemeral` (SandboxExecutor) |
|---|---|---|---|
| Event types | same canonical set | same canonical set | same canonical set |
| `data` keys | same as above | same as above | same as above (source of the contract) |
| `result` events per run | one, with aggregate usage | one, with aggregate usage | one per agent turn, with per-turn usage |
| `cost_usd` | `null` | `null` | real per-turn cost |
| `ts` | run completion time for all events | run completion time for all events | real per-event time |
| `error` events | on failure, with the failure message | on failure, with the failure message | on failure, with the failure message |

### Documented gaps (none/local vs ephemeral)

- **Per-turn usage**: pydantic-ai exposes only aggregate `usage` on a run
  result. `none`/`local` emit exactly one `result` event per run with
  aggregate `input_tokens`/`output_tokens`; ephemeral emits one `result`
  event per agent turn. Consumers summing `result`-event usage (e.g.
  `sandbox.py` `_sum_result_event_usage`) get the same totals either way.
- **Cost**: `cost_usd` is `null` for `none`/`local` (unknown — not faked
  as `0`); ephemeral reports the real per-turn cost.
- **Timestamps**: reconstructed events carry the run completion
  timestamp; per-event wall-clock times are only available in ephemeral.
- **Thinking granularity**: ephemeral buffers streaming thinking deltas;
  reconstruction works from the final message history, so thinking is
  complete-block only.

Streaming responses (`StreamEvent` token deltas) are a separate
transport concern and are not part of this transcript contract.

## Normalization and persistence

`normalize_transcript_events` (`cloud_agents.workflow.core.models`)
passes canonical events through unchanged and maps legacy flat executor
summaries (pre-parity `llm.call` / `agent.run` / `agent.stream` entries
and `[{role: user}, {role: assistant}]` pairs) to `result` events for
backward compatibility. The `TranscriptStorePersistenceMiddleware`
persists each step's `StepTranscript` (events + aggregate usage) through
the workflow transcript store; one-step and multi-step workflows use the
same path (`GET /v1/workflows/{id}/transcripts` in lightspeed-stack).
