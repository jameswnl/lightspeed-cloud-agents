"""Tests for skills capability loading — pydantic-ai-skills integration."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch


class TestGetSkillsCapability:
    """Tests for get_skills_capability() function."""

    def test_returns_none_when_env_var_unset(self) -> None:
        """Returns None when CLOUD_AGENTS_SKILLS_PATHS is not set."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        env = os.environ.copy()
        env.pop(SKILLS_PATHS_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            result = get_skills_capability()

        assert result is None

    def test_returns_none_when_env_var_empty(self) -> None:
        """Returns None when CLOUD_AGENTS_SKILLS_PATHS is empty string."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with patch.dict(os.environ, {SKILLS_PATHS_ENV: ""}, clear=False):
            result = get_skills_capability()

        assert result is None

    def test_returns_none_when_env_var_only_colons(self) -> None:
        """Returns None when CLOUD_AGENTS_SKILLS_PATHS is just colons/whitespace."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with patch.dict(os.environ, {SKILLS_PATHS_ENV: ": : :"}, clear=False):
            result = get_skills_capability()

        assert result is None

    def test_returns_skills_capability_with_single_path(self) -> None:
        """Returns SkillsCapability when a single valid path is configured."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {SKILLS_PATHS_ENV: tmpdir}, clear=False):
                result = get_skills_capability()

            assert result is not None
            from pydantic_ai_skills import SkillsCapability

            assert isinstance(result, SkillsCapability)

    def test_handles_colon_separated_paths(self) -> None:
        """Correctly splits colon-separated paths."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with (
            tempfile.TemporaryDirectory() as d1,
            tempfile.TemporaryDirectory() as d2,
        ):
            with patch.dict(
                os.environ,
                {SKILLS_PATHS_ENV: f"{d1}:{d2}"},
                clear=False,
            ):
                result = get_skills_capability()

            assert result is not None
            from pydantic_ai_skills import SkillsCapability

            assert isinstance(result, SkillsCapability)

    def test_strips_whitespace_from_paths(self) -> None:
        """Strips whitespace from individual paths."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {SKILLS_PATHS_ENV: f" {tmpdir} "},
                clear=False,
            ):
                result = get_skills_capability()

            assert result is not None

    def test_skips_nonexistent_paths(self) -> None:
        """Skips non-existent directories with warning, returns None if all invalid."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with patch.dict(
            os.environ,
            {SKILLS_PATHS_ENV: "/nonexistent/path1:/nonexistent/path2"},
            clear=False,
        ):
            result = get_skills_capability()

        assert result is None

    def test_graceful_when_pydantic_ai_skills_not_installed(self) -> None:
        """Returns None and logs warning when pydantic-ai-skills is not importable."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.dict(os.environ, {SKILLS_PATHS_ENV: tmpdir}, clear=False),
                patch(
                    "cloud_agents.workflow.executor.step.skills._import_skills_capability",
                    side_effect=ImportError("No module named 'pydantic_ai_skills'"),
                ),
            ):
                result = get_skills_capability()

        assert result is None

    def test_env_var_constant_value(self) -> None:
        """SKILLS_PATHS_ENV constant has expected value."""
        from cloud_agents.workflow.executor.step.skills import SKILLS_PATHS_ENV

        assert SKILLS_PATHS_ENV == "CLOUD_AGENTS_SKILLS_PATHS"

    def test_returns_none_for_empty_include(self) -> None:
        """get_skills_capability(include=[]) returns None (no skills requested)."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {SKILLS_PATHS_ENV: tmpdir}, clear=False):
                result = get_skills_capability(include=[])

            assert result is None

    def test_passes_include_to_skills_capability(self) -> None:
        """include=[...] is forwarded to SkillsCapability(include=...)."""
        from unittest.mock import MagicMock

        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_cls = MagicMock()
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            with (
                patch.dict(os.environ, {SKILLS_PATHS_ENV: tmpdir}, clear=False),
                patch(
                    "cloud_agents.workflow.executor.step.skills._import_skills_capability",
                    return_value=mock_cls,
                ),
            ):
                result = get_skills_capability(include=["k8s-diag"])

            expected_dir = os.path.realpath(tmpdir)
            mock_cls.assert_called_once_with(directories=[expected_dir], include=["k8s-diag"], validate=False)
            assert result is mock_instance
