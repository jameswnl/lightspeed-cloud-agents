# Identity Model: session_id, workflow_id, conversation_id, trace_id, user_id

How the identity fields relate to each other and to the database schema.
Originally designed in [#146](https://github.com/jameswnl/lightspeed-cloud-agents/issues/146)
(closed via #149); this doc makes that design discoverable without digging
through the issue. See also `docs/ARCHITECTURE.md` (Observability section)
and the "Identity model (StepMetadata)" section of the root `CLAUDE.md`.

## Identity fields

| Field | Set by | Scope | Persisted? | Required? |
|---|---|---|---|---|
| `workflow_id` | Caller or generated | One workflow run | `workflow_run_state` PK; `step_transcripts` (unique with `step_name`) | Yes |
| `conversation_id` | `ChatWorkflowRunner` | **Alias for `workflow_id`** in chat mode — not a distinct ID | No — never stored as a column, see below | N/A |
| `user_id` | Caller | Whoever initiated a given workflow row | `workflow_run_state.user_id` | No |
| `session_id` | Caller | Groups related workflow rows | `workflow_run_state.session_id` | No — nullable, caller convention only |
| `trace_id` | OTEL | Correlates a step's stored transcript with its OTEL trace | `step_transcripts.trace_id` | No |

These live on `StepMetadata` (`workflow/executor/step/base.py`), carried via
`StepInput.metadata`.

**`conversation_id` is never persisted.** It exists only on `StepMetadata`
and as the `{conversation_id}` path segment in the chat HTTP API
(`workflow/executor/chat/api.py`) — `ChatWorkflowRunner` sets
`conversation_id=workflow_id` directly (`runner.py`). There is no
"conversations" table; if you're looking for where a conversation's turns
live, that's `step_transcripts`, keyed by `workflow_id`. Don't build a
separate conversations table or model — keep this alias intact if you touch
`ChatWorkflowRunner`.

**This alias is a cloud-agents-internal convention, not a contract for
consumers.** lightspeed-stack integration (#145) has its own conversation
cache/compaction/RBAC layer; don't assume `conversation_id == workflow_id`
holds 1:1 across that boundary.

**There are two unrelated things called "session":** `workflow_run_state.session_id`
(nullable, caller-provided, groups related workflow rows) is not the same as
the CLI session concept in `workflow/cli_session.py` (`cli-sess-<uuid>`,
in-memory only, tracks a spawned CLI agent process). If you're working on
either, check which one you actually mean.

## Database tables

### `workflow_run_state` (`storage/run_state_store.py`)

Primary key `workflow_id`. Identity columns (`user_id`, `session_id`,
`parent_workflow_id`) were added by the Alembic migration
`002_identity_model`, layered on top of the `001_baseline` migration.
Alembic is the sole schema owner (#169) — the store itself has no
`CREATE TABLE` fallback; `connect()` fails loudly via `run_alembic()` if
migrations can't be applied.

```text
workflow_id          TEXT PRIMARY KEY
workflow_name        TEXT NOT NULL
status                TEXT NOT NULL DEFAULT 'running'
...
user_id              TEXT            -- added in 002_identity_model
session_id           TEXT            -- added in 002_identity_model
parent_workflow_id   TEXT            -- added in 002_identity_model
```

Indexed on all three (`idx_wrs_user`, `idx_wrs_session`, `idx_wrs_parent`).
Query helpers: `RunStateStore.list_by_user(user_id)`,
`RunStateStore.list_by_session(session_id)`.

`parent_workflow_id` is meant for escalation chains (a human-takeover
workflow pointing back at the agent workflow it escalated from), but it is a
plain nullable `TEXT` column with an index — **not a database foreign key**
(no `REFERENCES workflow_run_state(workflow_id)`), so nothing enforces that
it points at a real row. It is also **pass-through only** today: it's
accepted by `RunStateStore.create()` and `LocalWorkflowRunner`, but nothing
reads or traverses it for escalation logic. Escalation behavior itself was
explicitly deferred out of #146 to a future issue.

`workflow_context` is a schema-less JSONB column (part of the base schema,
predating the identity model) used as a grab-bag for orchestration-level
data — `LocalWorkflowRunner` uses it to carry `sandbox_image`,
`skills_image`, `mcp_servers`, `approval_policy`, and (since #179)
`trace_parent`: the W3C traceparent of the last step executed before a
pause, used to link the first post-resume step's span back to the pre-pause
trace via an OTEL span Link (not a shared `trace_id` — see
`workflow/executor/middleware.py::MiddlewareExecutor` and
`workflow/executor/local/executor.py::_resume_from_store`).
`update_workflow_context()` replaces the whole column, so callers must read
before merging in a new key rather than overwriting it.

### `step_transcripts` (`storage/transcript_store.py`)

Primary key is a `SERIAL id`, with a `UNIQUE(workflow_id, step_name)`
constraint — one row per step within a workflow.

```text
id             SERIAL PRIMARY KEY
workflow_id    TEXT NOT NULL
step_name      TEXT NOT NULL
events         JSONB NOT NULL
trace_id       TEXT            -- added in 002_identity_model, OTEL correlation
messages       JSONB           -- added in 002_identity_model, ConversationMessage[]
...
UNIQUE(workflow_id, step_name)
```

No `user_id` / `session_id` / `conversation_id` columns here. **There is no
foreign key or join between this table and `workflow_run_state`** — both
tables are keyed independently by the same `workflow_id` string, by
convention only. Orphan `step_transcripts` rows are possible:
`ChatWorkflowRunner.send_message()` only blocks on a *terminal* run state
(`_check_not_terminal`) — if no `workflow_run_state` row exists at all for
that `workflow_id`, it proceeds and writes a transcript anyway.

`trace_id` and `messages` upsert with `COALESCE` against the existing row so
a later write without a value doesn't wipe one already stored.

`TranscriptStore.load_recent_turns(workflow_id, limit)` reads the `messages`
column to reconstruct chat history per `workflow_id`, and is what
`ChatWorkflowRunner` calls to rebuild context on each turn.

## Middleware and identity

Cross-cutting concerns around step execution are implemented as
`StepMiddleware` in `workflow/executor/middleware.py`. The two concrete
implementations today are `TracingMiddleware` (OTEL spans) and
`TranscriptMiddleware` (writes to `TranscriptStore`). There is no quota
middleware in this repo. Audit logging is a separate mechanism —
`runtime.audit.emit_audit`, called from the API/activities/CLI layers — not
a `StepMiddleware`.

## Putting it together

```text
session_id: "ses-abc"          (groups related rows, optional, not enforced)
├── workflow_id: "wf-1"        (== conversation_id in chat mode)
│   ├── user_id: "jwong"
│   └── step_transcripts rows, keyed by (wf-1, step_name), messages JSONB
│
└── workflow_id: "wf-2"        (e.g. human escalation takeover)
    ├── user_id: "oncall-engineer"     (can differ from wf-1's user_id)
    ├── parent_workflow_id: "wf-1"     (app-level pointer, not an FK, not yet traversed)
    └── step_transcripts rows, keyed by (wf-2, step_name)
```

`user_id` is a column on each `workflow_run_state` row, not a session-level
property — two workflows sharing a `session_id` can have different
`user_id`s, and nothing constrains that.

## Things to know when working in this area

- `session_id` is not enforced or auto-generated anywhere — if a caller
  omits it, workflows just have `session_id = NULL` and won't show up under
  `list_by_session`.
- Alembic (`alembic/versions/`) is the sole schema owner for both tables —
  the stores have no `CREATE TABLE` fallback (removed in #169).
  `connect()` raises if migrations can't be applied (e.g. missing
  `alembic.ini`) rather than silently running with a stale/partial schema.
  See "Database Migrations" in the root `CLAUDE.md`.
- If you implement escalation, `parent_workflow_id` already has the column
  and index — the missing pieces are: an FK/validity check if you want one,
  application logic to set it meaningfully, and a way to traverse the chain
  (e.g. a `list_by_parent` store method, surfacing "escalated from" in the
  API/UI).
