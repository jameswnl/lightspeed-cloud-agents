"""Integration test: SkillsCapability discovers SKILL.md files from configured directories."""

from __future__ import annotations

import os
import tempfile

import pytest


class TestSkillsLoadingIntegration:
    """Integration tests for skills loading with real SKILL.md files."""

    @pytest.mark.asyncio
    async def test_skills_discovered_from_directory(self) -> None:
        """SkillsCapability discovers SKILL.md and the skill name is accessible."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Layout: tmpdir/test-skill/SKILL.md
            skill_dir = os.path.join(tmpdir, "test-skill")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
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

                from pydantic_ai_skills import SkillsCapability

                assert isinstance(cap, SkillsCapability)

                # Verify the skill was actually discovered
                skill_names = list(cap._toolset.skills.keys())
                assert "test-skill" in skill_names
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old

    @pytest.mark.asyncio
    async def test_multiple_skills_discovered(self) -> None:
        """SkillsCapability loads multiple skills from multiple directories."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with (
            tempfile.TemporaryDirectory() as dir1,
            tempfile.TemporaryDirectory() as dir2,
        ):
            # Layout: dir1/skill-one/SKILL.md
            skill_one = os.path.join(dir1, "skill-one")
            os.makedirs(skill_one)
            with open(os.path.join(skill_one, "SKILL.md"), "w") as f:
                f.write(
                    "---\n"
                    "name: skill-one\n"
                    "description: First skill\n"
                    "---\n"
                    "First skill content.\n"
                )

            # Layout: dir2/skill-two/SKILL.md
            skill_two = os.path.join(dir2, "skill-two")
            os.makedirs(skill_two)
            with open(os.path.join(skill_two, "SKILL.md"), "w") as f:
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

                skill_names = list(cap._toolset.skills.keys())
                assert "skill-one" in skill_names
                assert "skill-two" in skill_names
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old

    @pytest.mark.asyncio
    async def test_empty_directory_no_skills(self) -> None:
        """Empty directory produces a SkillsCapability with no skills."""
        from cloud_agents.workflow.executor.step.skills import (
            SKILLS_PATHS_ENV,
            get_skills_capability,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            old = os.environ.get(SKILLS_PATHS_ENV)
            try:
                os.environ[SKILLS_PATHS_ENV] = tmpdir
                cap = get_skills_capability()
                assert cap is not None

                skill_names = list(cap._toolset.skills.keys())
                assert len(skill_names) == 0
            finally:
                if old is None:
                    os.environ.pop(SKILLS_PATHS_ENV, None)
                else:
                    os.environ[SKILLS_PATHS_ENV] = old
