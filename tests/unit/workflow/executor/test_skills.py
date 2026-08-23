"""Tests for skills capability loading — pydantic-ai-skills integration."""

from __future__ import annotations

import os
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
        """Returns SkillsCapability when a single path is configured."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with patch.dict(os.environ, {SKILLS_PATHS_ENV: "/tmp/skills"}, clear=False):
            result = get_skills_capability()

        assert result is not None
        # SkillsCapability should have been created with the path
        from pydantic_ai_skills import SkillsCapability

        assert isinstance(result, SkillsCapability)

    def test_handles_colon_separated_paths(self) -> None:
        """Correctly splits colon-separated paths."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with patch.dict(
            os.environ,
            {SKILLS_PATHS_ENV: "/tmp/skills1:/tmp/skills2:/tmp/skills3"},
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

        with patch.dict(
            os.environ,
            {SKILLS_PATHS_ENV: " /tmp/skills1 : /tmp/skills2 "},
            clear=False,
        ):
            result = get_skills_capability()

        assert result is not None

    def test_graceful_when_pydantic_ai_skills_not_installed(self) -> None:
        """Returns None and logs warning when pydantic-ai-skills is not importable."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with (
            patch.dict(os.environ, {SKILLS_PATHS_ENV: "/tmp/skills"}, clear=False),
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
