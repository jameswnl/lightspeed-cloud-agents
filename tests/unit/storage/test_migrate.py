"""Tests for the shared Alembic migration helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestAlembicIniPath:
    """Tests that alembic.ini is resolved correctly."""

    def test_alembic_ini_exists(self) -> None:
        """_ALEMBIC_INI points to an existing file in the repo root."""
        from cloud_agents.storage.migrate import _ALEMBIC_INI

        assert _ALEMBIC_INI.exists(), f"alembic.ini not found at {_ALEMBIC_INI}"

    def test_alembic_ini_is_in_repo_root(self) -> None:
        """_ALEMBIC_INI is in the same directory as pyproject.toml."""
        from cloud_agents.storage.migrate import _ALEMBIC_INI

        repo_root = _ALEMBIC_INI.parent
        assert (repo_root / "pyproject.toml").exists()


class TestRunAlembic:
    """Tests for run_alembic() helper."""

    def test_raises_when_alembic_not_installed(self) -> None:
        """run_alembic raises when alembic isn't importable.

        Alembic is a required dependency (so this is effectively dead
        code in a correctly installed environment), but with no
        CREATE TABLE fallback in the stores anymore, silently continuing
        here would defer to a confusing "relation does not exist" error
        on the first query instead of a clear one now.
        """
        from cloud_agents.storage.migrate import run_alembic

        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def mock_import(name, *args, **kwargs):
            if name in ("alembic", "alembic.command", "alembic.config"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(RuntimeError, match="alembic is not importable"):
                run_alembic("postgresql://localhost/testdb")

    def test_asyncpg_url_translated(self) -> None:
        """+asyncpg is stripped from URL for sync driver."""
        from cloud_agents.storage.migrate import run_alembic

        with (
            patch("cloud_agents.storage.migrate._ALEMBIC_INI") as mock_ini,
            patch("alembic.command.upgrade") as mock_upgrade,
            patch("alembic.config.Config") as mock_config_cls,
            patch.dict("os.environ", {}, clear=False),
        ):
            mock_ini.exists.return_value = True
            mock_ini.__str__ = MagicMock(return_value="/fake/alembic.ini")

            run_alembic("postgresql+asyncpg://localhost/testdb")

            import os

            assert os.environ.get("RUN_STATE_DB_URL") == "postgresql://localhost/testdb"

    def test_migration_failure_raises_runtime_error(self) -> None:
        """Failed migration raises RuntimeError, not silently continues."""
        from cloud_agents.storage.migrate import run_alembic

        with (
            patch("cloud_agents.storage.migrate._ALEMBIC_INI") as mock_ini,
            patch("alembic.command.upgrade", side_effect=Exception("revision conflict")),
            patch("alembic.config.Config"),
            patch.dict("os.environ", {}, clear=False),
        ):
            mock_ini.exists.return_value = True
            mock_ini.__str__ = MagicMock(return_value="/fake/alembic.ini")

            with pytest.raises(RuntimeError, match="revision conflict"):
                run_alembic("postgresql://localhost/testdb")

    def test_missing_ini_raises(self) -> None:
        """Missing alembic.ini raises RuntimeError rather than silently skipping.

        Alembic is the sole schema owner (#169) -- the stores have no
        CREATE TABLE fallback, so a misconfigured deployment (e.g. missing
        alembic.ini in a non-source-checkout install) must fail loudly here
        rather than defer to a confusing "relation does not exist" error
        on the first query.
        """
        from cloud_agents.storage.migrate import run_alembic

        with patch("cloud_agents.storage.migrate._ALEMBIC_INI") as mock_ini:
            mock_ini.exists.return_value = False

            with pytest.raises(RuntimeError, match=r"alembic\.ini not found"):
                run_alembic("postgresql://localhost/testdb")
