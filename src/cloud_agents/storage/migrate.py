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
        logger.debug("alembic.ini not found at %s, skipping migrations", _ALEMBIC_INI)
        return

    sync_url = db_url.replace("+asyncpg", "") if "+asyncpg" in db_url else db_url
    os.environ["RUN_STATE_DB_URL"] = sync_url

    try:
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied")
    except Exception as exc:
        raise RuntimeError(f"Alembic migration failed: {exc}") from exc
