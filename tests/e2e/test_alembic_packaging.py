"""E2E test: Alembic migration assets are actually shippable (#188 bug 3).

Regression test for: alembic.ini and alembic/versions/ lived at the repo
root, outside src/cloud_agents/. The wheel build only packages
src/cloud_agents (pyproject.toml's [tool.hatch.build.targets.wheel]), and
Containerfiles that COPY src/ (this repo's deploy/workflow-runner/
Containerfile, lightspeed-stack's Containerfile.harness) don't pick up
repo-root files either way. A real Kind deployment shipped with a stale
schema and no way to run migrations from inside the container as a result.

This builds a real wheel and installs it into a clean venv with no access
to the source checkout, then verifies migrations can actually run from
that installed package alone -- proving the fix, not just that the files
moved. Runs against a throwaway database created and dropped by the test,
never against a shared/pre-existing one.

Requires: uv (already required to run this test suite), a reachable
PostgreSQL admin connection (ADMIN_DB_URL, defaults to the local dev
Postgres) with permission to CREATE DATABASE / DROP DATABASE.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
ADMIN_DB_URL = os.environ.get(
    "ADMIN_DB_URL", "postgresql://lightspeed:lightspeed@localhost:5432/lightspeed"
)


def _admin_connection_available() -> bool:
    """Check if the configured PostgreSQL admin connection is reachable."""
    try:
        import asyncpg
    except ImportError:
        return False

    async def _check() -> bool:
        try:
            conn = await asyncio.wait_for(asyncpg.connect(ADMIN_DB_URL), timeout=2)
        except Exception:
            return False
        await conn.close()
        return True

    try:
        return asyncio.run(_check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _admin_connection_available(),
    reason=f"Packaging e2e test requires a reachable PostgreSQL admin connection at {ADMIN_DB_URL}",
)


@pytest.fixture
def throwaway_db_url() -> str:
    """Create a uniquely-named throwaway database, yield its URL, then drop it."""
    import asyncpg

    db_name = f"cloud_agents_pkg_test_{uuid.uuid4().hex[:12]}"

    async def _create() -> None:
        conn = await asyncpg.connect(ADMIN_DB_URL)
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    async def _drop() -> None:
        conn = await asyncpg.connect(ADMIN_DB_URL)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    base = ADMIN_DB_URL.rsplit("/", 1)[0]
    try:
        yield f"{base}/{db_name}"
    finally:
        asyncio.run(_drop())


def test_wheel_includes_alembic_assets(tmp_path: Path) -> None:
    """The built wheel's file list includes alembic.ini and both migrations."""
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"Expected exactly one wheel, found {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())

    assert "cloud_agents/_alembic/alembic.ini" in names
    assert "cloud_agents/_alembic/alembic/env.py" in names
    assert "cloud_agents/_alembic/alembic/versions/001_baseline.py" in names
    assert "cloud_agents/_alembic/alembic/versions/002_identity_model.py" in names


def test_migrations_run_from_clean_wheel_install(tmp_path: Path, throwaway_db_url: str) -> None:
    """Migrations succeed using only an installed wheel -- no source checkout.

    This is the test that would have caught #188 bug 3 before it shipped:
    it proves cloud_agents.storage.migrate.run_alembic() can locate and
    apply migrations when the package is installed the way a real
    (non-editable) production image installs it, not just when run from
    a source checkout. Runs against a throwaway database, not a shared one.
    """
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(wheel_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    venv_python = venv_dir / "bin" / "python3"

    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheels[0])],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Run from outside the repo so there's no accidental fallback onto the
    # source checkout's alembic.ini or an ambient PYTHONPATH.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    script = (
        "import asyncio\n"
        "from cloud_agents.storage.run_state_store import RunStateStore\n"
        "async def main():\n"
        f"    store = RunStateStore(db_url={throwaway_db_url!r})\n"
        "    await store.connect()\n"
        "    await store.close()\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [str(venv_python), "-c", script],
        cwd=run_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"Migration from clean wheel install failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
