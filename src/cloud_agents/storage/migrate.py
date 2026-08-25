"""Shared Alembic migration helper for both stores.

Runs Alembic upgrade head from connect() to ensure identity columns
and other schema changes are applied before the store operates.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def run_alembic(db_url: str) -> None:
    """Run Alembic migrations against the given database.

    Parameters:
        db_url: PostgreSQL connection URL (asyncpg or psycopg2 format).

    Raises:
        RuntimeError: If Alembic is configured but migrations fail.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        logger.debug("Alembic not installed, skipping migrations")
        return

    if not _ALEMBIC_INI.exists():
        # Unlike "alembic not installed" (a base dependency, effectively
        # dead code) and "DB unreachable" (the next line will fail loudly
        # anyway), a missing alembic.ini is always a real misconfiguration:
        # it means this isn't running from a full source checkout (e.g. a
        # non-editable/wheel install), so Alembic can never apply the
        # schema. The stores no longer have a CREATE TABLE fallback for
        # this -- silently skipping here would mean every query fails
        # later with "relation does not exist" instead of a clear error now.
        raise RuntimeError(
            f"alembic.ini not found at {_ALEMBIC_INI} -- cannot apply the "
            "database schema. cloud_agents must be run from a full source "
            "checkout with alembic.ini present at the repo root."
        )

    sync_url = db_url.replace("+asyncpg", "") if "+asyncpg" in db_url else db_url
    os.environ["RUN_STATE_DB_URL"] = sync_url

    try:
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied")
    except Exception as exc:
        # Connection errors (no PostgreSQL) are expected in unit tests
        # and environments where the database isn't ready yet.
        # Schema/revision errors should be visible.
        exc_str = str(exc).lower()
        if "connection refused" in exc_str or "could not connect" in exc_str or "no password" in exc_str:
            logger.debug("Alembic migration skipped (database unavailable): %s", exc)
        else:
            raise RuntimeError(f"Alembic migration failed: {exc}") from exc
