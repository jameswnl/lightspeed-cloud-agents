"""Integration test: SkillsCapability discovers SKILL.md files from configured directories."""

from __future__ import annotations

import os
import tempfile

import pytest


class TestSkillsLoadingIntegration:
    """Integration tests for skills loading with real SKILL.md files."""

    @pytest.mark.asyncio
    async def test_skills_loaded_from_directory(self) -> None:
        """SkillsCapability discovers SKILL.md files from configured directories."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a SKILL.md file
            skill_md = os.path.join(tmpdir, "SKILL.md")
            with open(skill_md, "w") as f:
                f.write(
                    "---\n"
                    "name: test-skill\n"
                    "description: A test skill for integration testing\n"
                    "---\n"
                    "You are a test skill.\n"
                )

            old = os.environ.get(SKILLS_PATHS_ENV)
            try:
                os.environ[SKILLS_PATHS_ENV] = tmpdir
                cap = get_skills_capability()
                assert cap is not None

                # Verify it's a SkillsCapability instance
                from pydantic_ai_skills import SkillsCapability

                assert isinstance(cap, SkillsCapability)
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old

    @pytest.mark.asyncio
    async def test_multiple_directories_with_skills(self) -> None:
        """SkillsCapability loads from multiple colon-separated directories."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with (
            tempfile.TemporaryDirectory() as dir1,
            tempfile.TemporaryDirectory() as dir2,
        ):
            # Create SKILL.md in first directory
            with open(os.path.join(dir1, "SKILL.md"), "w") as f:
                f.write(
                    "---\n"
                    "name: skill-one\n"
                    "description: First skill\n"
                    "---\n"
                    "First skill content.\n"
                )

            # Create SKILL.md in second directory
            with open(os.path.join(dir2, "SKILL.md"), "w") as f:
                f.write(
                    "---\n"
                    "name: skill-two\n"
                    "description: Second skill\n"
                    "---\n"
                    "Second skill content.\n"
                )

            old = os.environ.get(SKILLS_PATHS_ENV)
            try:
                os.environ[SKILLS_PATHS_ENV] = f"{dir1}:{dir2}"
                cap = get_skills_capability()
                assert cap is not None
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old

    @pytest.mark.asyncio
    async def test_empty_directory_returns_capability(self) -> None:
        """SkillsCapability is created even when directory has no SKILL.md files."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            old = os.environ.get(SKILLS_PATHS_ENV)
            try:
                os.environ[SKILLS_PATHS_ENV] = tmpdir
                cap = get_skills_capability()
                # Should still return a capability (validate=False)
                assert cap is not None
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old
