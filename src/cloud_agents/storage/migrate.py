"""Shared Alembic migration helper for both stores.

Runs Alembic upgrade head from connect() to ensure identity columns
and other schema changes are applied before the store operates.

Known limitation: _ALEMBIC_INI resolves relative to this file's location
in a source tree. alembic.ini and alembic/versions/ live at the repo root,
outside src/cloud_agents/, and the wheel build only packages
src/cloud_agents (see [tool.hatch.build.targets.wheel] in pyproject.toml).
This means Alembic can only actually run when cloud_agents is installed
from a full source checkout (including editable installs, which don't
relocate files) -- a real (non-editable) wheel install will always hit
the RuntimeError below. Packaging the Alembic assets so a wheel install
can run migrations too is tracked as a separate concern from #169.
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
        RuntimeError: If Alembic can't be run at all (not installed,
            alembic.ini not found) or migrations fail (e.g. a revision
            conflict). A genuinely unreachable database is not raised
            here -- see the module docstring.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        # alembic is a required (non-optional) dependency, so this should
        # never fire in a correctly installed environment -- but if it
        # does, the stores have no CREATE TABLE fallback anymore, so
        # silently continuing here would mean every query fails later
        # with "relation does not exist" instead of a clear error now.
        raise RuntimeError(
            "alembic is not importable -- cannot apply the database schema. "
            "alembic is a required dependency; check the installed environment."
        ) from exc

    if not _ALEMBIC_INI.exists():
        # A missing alembic.ini means this isn't running from a full source
        # checkout (see module docstring) -- Alembic can never apply the
        # schema. Same reasoning as the ImportError case above: fail loudly
        # now rather than defer to a confusing failure on the first query.
        raise RuntimeError(
            f"alembic.ini not found at {_ALEMBIC_INI} -- cannot apply the "
            "database schema. cloud_agents must be run from a full source "
            "checkout (including editable installs) with alembic.ini "
            "present at the repo root."
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
