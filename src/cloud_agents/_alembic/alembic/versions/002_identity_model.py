"""Add identity columns for user tracking, OTEL correlation, and conversation support.

Revision ID: 002_identity_model
Revises: 001_baseline
Create Date: 2026-08-23

Adds identity columns to workflow_run_state (user_id, session_id,
parent_workflow_id) and step_transcripts (trace_id, messages).
Uses IF NOT EXISTS / IF EXISTS for idempotent execution.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision = "002_identity_model"
down_revision = "001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add identity and conversation columns."""
    # RunStateStore: identity columns
    op.execute("ALTER TABLE workflow_run_state ADD COLUMN IF NOT EXISTS user_id TEXT;")
    op.execute("ALTER TABLE workflow_run_state ADD COLUMN IF NOT EXISTS session_id TEXT;")
    op.execute("ALTER TABLE workflow_run_state ADD COLUMN IF NOT EXISTS parent_workflow_id TEXT;")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wrs_user ON workflow_run_state(user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wrs_session ON workflow_run_state(session_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_wrs_parent ON workflow_run_state(parent_workflow_id);"
    )

    # TranscriptStore: OTEL correlation + conversation messages
    op.execute("ALTER TABLE step_transcripts ADD COLUMN IF NOT EXISTS trace_id TEXT;")
    op.execute("ALTER TABLE step_transcripts ADD COLUMN IF NOT EXISTS messages JSONB;")


def downgrade() -> None:
    """Remove identity and conversation columns."""
    op.execute("ALTER TABLE step_transcripts DROP COLUMN IF EXISTS messages;")
    op.execute("ALTER TABLE step_transcripts DROP COLUMN IF EXISTS trace_id;")
    op.execute("DROP INDEX IF EXISTS idx_wrs_parent;")
    op.execute("DROP INDEX IF EXISTS idx_wrs_session;")
    op.execute("DROP INDEX IF EXISTS idx_wrs_user;")
    op.execute("ALTER TABLE workflow_run_state DROP COLUMN IF EXISTS parent_workflow_id;")
    op.execute("ALTER TABLE workflow_run_state DROP COLUMN IF EXISTS session_id;")
    op.execute("ALTER TABLE workflow_run_state DROP COLUMN IF EXISTS user_id;")
