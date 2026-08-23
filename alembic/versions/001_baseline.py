"""Baseline: workflow_run_state and step_transcripts tables.

Revision ID: 001_baseline
Revises: None
Create Date: 2026-08-23

Represents the existing schema as a migration baseline.
Uses CREATE TABLE IF NOT EXISTS so it is safe to run against
databases that already have these tables.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision = "001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create baseline tables for workflow state and transcripts."""
    op.execute("""
    CREATE TABLE IF NOT EXISTS workflow_run_state (
        workflow_id TEXT PRIMARY KEY,
        workflow_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        current_step TEXT,
        steps JSONB NOT NULL DEFAULT '{}',
        events JSONB NOT NULL DEFAULT '[]',
        definition JSONB NOT NULL DEFAULT '{}',
        provider JSONB NOT NULL DEFAULT '{}',
        authz_context JSONB NOT NULL DEFAULT '{}',
        workflow_context JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_workflow_run_state_status
        ON workflow_run_state(status);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS step_transcripts (
        id SERIAL PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        step_name TEXT NOT NULL,
        events JSONB NOT NULL,
        cost_usd DOUBLE PRECISION,
        input_tokens INTEGER,
        output_tokens INTEGER,
        duration_ms INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(workflow_id, step_name)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_step_transcripts_workflow
        ON step_transcripts(workflow_id);
    """)


def downgrade() -> None:
    """Drop baseline tables."""
    op.execute("DROP TABLE IF EXISTS step_transcripts;")
    op.execute("DROP TABLE IF EXISTS workflow_run_state;")
