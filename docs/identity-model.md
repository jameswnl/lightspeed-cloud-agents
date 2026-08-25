# Identity Model: session_id, workflow_id, conversation_id, trace_id

How the four identity fields relate to each other and to the database schema.
Originally designed in [#146](https://github.com/jameswnl/lightspeed-cloud-agents/issues/146)
(closed via #149); this doc makes that design discoverable without digging
through the issue.

## The four IDs

| ID | Set by | Scope | Required? |
|---|---|---|---|
| `workflow_id` | Caller or generated | One workflow run (primary key everywhere) | Yes |
| `conversation_id` | `ChatWorkflowRunner` | **Alias for `workflow_id`** in chat mode — not a distinct ID | N/A |
| `session_id` | Caller | Groups related workflows (e.g. chat → escalation → resolution) | No — nullable, caller convention only |
| `trace_id` | OTEL | Correlates a step's stored transcript with its OTEL trace | No |
| `user_id` | Caller | Identity of whoever initiated the workflow | No |

These live on `StepMetadata`
(`src/cloud_agents/workflow/executor/step/base.py:19-34`), carried via
`StepInput.metadata`. `StepMiddleware` implementations (tracing, audit, quota)
read/populate this struct — see `ARCHITECTURE.md` for the middleware chain.

**Key point:** there is no separate "conversation" table or concept.
`ChatWorkflowRunner` sets `conversation_id=workflow_id` directly
(`workflow/executor/chat/runner.py:580`), and the chat HTTP API
(`workflow/executor/chat/api.py:45-104`) treats the `conversation_id` path
param as literally the `workflow_id`. If you're looking for "the conversation
table," it's `step_transcripts`, keyed by `workflow_id`.

## Database tables

### `workflow_run_state` (`storage/run_state_store.py`)

Primary key `workflow_id`. Identity columns (`user_id`, `session_id`,
`parent_workflow_id`) were added by Alembic migration
`alembic/versions/002_identity_model.py`, not the original
`CREATE TABLE IF NOT EXISTS` in the store (that block predates the identity
model and is kept only as a legacy fallback schema — see the store's module
docstring).

```sql
workflow_id          TEXT PRIMARY KEY
workflow_name        TEXT NOT NULL
status                TEXT NOT NULL DEFAULT 'running'
...
user_id              TEXT            -- added in 002_identity_model
session_id           TEXT            -- added in 002_identity_model
parent_workflow_id   TEXT            -- added in 002_identity_model, self-referencing
```

Indexed on all three (`idx_wrs_user`, `idx_wrs_session`, `idx_wrs_parent`).
Query helpers: `list_by_user(user_id)` (`run_state_store.py:438`),
`list_by_session(session_id)` (`run_state_store.py:454`).

`parent_workflow_id` is for escalation chains (a human-takeover workflow
pointing back at the agent workflow it escalated from). **The column and
index exist but nothing currently reads or writes it for escalation logic**
beyond passing it through on `create()` (`run_state_store.py:194,224`) and
`LocalWorkflowRunner` (`workflow/executor/local/executor.py:88`). Escalation
behavior itself was explicitly deferred out of #146 to a future issue.

### `step_transcripts` (`storage/transcript_store.py`)

Keyed by `(workflow_id, step_name)` unique constraint — one row per step
within a workflow.

```sql
id             SERIAL PRIMARY KEY
workflow_id    TEXT NOT NULL
step_name      TEXT NOT NULL
events         JSONB NOT NULL
trace_id       TEXT            -- added in 002_identity_model, OTEL correlation
messages       JSONB           -- added in 002_identity_model, ConversationMessage[]
...
UNIQUE(workflow_id, step_name)
```

No `user_id` / `session_id` / `conversation_id` columns here — those are
looked up by joining back to `workflow_run_state.workflow_id`. `trace_id` and
`messages` upsert with `COALESCE` against the existing row
(`transcript_store.py:56-58`) so a later write without a value doesn't wipe
one already stored.

`load_recent_turns(workflow_id, limit)` (`transcript_store.py:268`) reads the
`messages` column to reconstruct chat history per `workflow_id`, and is what
`ChatWorkflowRunner` calls to rebuild context on each turn
(`runner.py:183,266,330`).

## Putting it together

```
session_id: "ses-abc"          (groups everything, optional)
├── user_id: "jwong"
├── workflow_id: "wf-1"        (== conversation_id in chat mode)
│   └── step_transcripts rows, keyed by (wf-1, step_name), messages JSONB
│
└── workflow_id: "wf-2"        (e.g. human escalation takeover)
    ├── parent_workflow_id: "wf-1"   (schema-ready, not yet wired to logic)
    └── step_transcripts rows, keyed by (wf-2, step_name)
```

## Things to know when working in this area

- `session_id` is not enforced or auto-generated anywhere — if a caller omits
  it, workflows just have `session_id = NULL` and won't show up under
  `list_by_session`.
- Don't build a separate "conversations" table or model — `conversation_id`
  and `workflow_id` are the same value by convention, not by a schema
  constraint, so keep that invariant intact if you touch `ChatWorkflowRunner`.
- The `CREATE TABLE IF NOT EXISTS` blocks in the stores are legacy
  base-schema fallbacks for pre-Alembic deployments and are missing the
  identity columns entirely — Alembic (`alembic/versions/*.py`) is the source
  of truth for the current schema. See "Database Migrations" in the root
  `CLAUDE.md`.
- If you implement escalation, `parent_workflow_id` already has the column
  and index — the missing piece is application logic to set it and to
  traverse the chain (e.g. a `list_by_parent` store method, and surfacing
  "escalated from" in the API/UI).
