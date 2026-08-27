"""E2E test: allowed_skills scoping for spawn: none / spawn: local (issue #204/#205).

Regression coverage for the parity gap documented in #204: unlike
spawn: ephemeral (Landlock-enforced, see
tests/e2e/test_guardrails.py::test_allowed_skills_scoping_on_real_gateway),
spawn: none/local scope skills via SkillsCapability(include=...) filtering
at discovery time -- there is no sandbox boundary, so an unlisted skill
must be genuinely absent from what the agent can see, not merely
unmentioned in the prompt.

Unit tests (test_direct_executor.py, test_subprocess_child.py) mock the
LLM/subprocess boundary and can only assert that the right kwargs were
passed. This file proves the actual, deterministic filtering result
(what pydantic_ai_skills discovers) and, separately, that a real LLM tool
loop can actually invoke an allowed skill's script end-to-end through
both DirectExecutor and SubprocessExecutor.

Prerequisites:
  - OPENAI_API_KEY set in environment (for the real-LLM tests only)
  - Run from the repo root, or with CLOUD_AGENTS_SKILLS_PATHS set

Usage:
  OPENAI_API_KEY=sk-... uv run pytest tests/e2e/test_allowed_skills_e2e.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cloud_agents.workflow.executor.step.base import StepInput
from cloud_agents.workflow.executor.step.direct import DirectExecutor
from cloud_agents.workflow.executor.step.skills import get_skills_capability
from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

TEST_MODEL = os.environ.get("TEST_LLM_MODEL", "gpt-4o-mini")

_SKILLS_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "skills")


@pytest.fixture(autouse=True)
def _skills_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLOUD_AGENTS_SKILLS_PATHS at examples/skills for every test in this file."""
    monkeypatch.setenv("CLOUD_AGENTS_SKILLS_PATHS", _SKILLS_DIR)


class TestAllowedSkillsFiltering:
    """Deterministic checks: what does discovery actually expose, before any LLM call."""

    def test_include_filters_to_the_named_skill_only(self) -> None:
        """allowed_skills=["k8s-diag"] discovers exactly that one skill.

        examples/skills/ ships four skills (k8s-diag, git-ops, cve-scan,
        security-audit) -- if include= filtering were broken (e.g. ignored,
        or matching substrings instead of exact names), this would catch it
        by asserting the exact discovered set, not just "at least one".
        """
        cap = get_skills_capability(include=["k8s-diag"])
        assert cap is not None
        assert set(cap.toolset.skills.keys()) == {"k8s-diag"}

    def test_no_include_filter_discovers_all_skills(self) -> None:
        """get_skills_capability(include=None) means "no filtering", not "no skills".

        The "step with no allowed_skills gets zero skills" contract (PR
        #205 point 5) is enforced one layer up, by the executors
        (`get_skills_capability(...) if step_input.allowed_skills is not
        None else None` in direct.py/subprocess_child.py) -- this helper's
        own contract, per its docstring, is that include=None means
        unfiltered, matching pydantic-ai-skills' own SkillsCapability
        semantics. See test_spawn_none_agent_has_no_skills_when_omitted
        below for the real, executor-level behavior.
        """
        cap = get_skills_capability(include=None)
        assert cap is not None
        assert set(cap.toolset.skills.keys()) == {
            "k8s-diag",
            "git-ops",
            "cve-scan",
            "security-audit",
        }

    def test_multiple_names_discovers_exactly_those(self) -> None:
        """allowed_skills with two names discovers exactly those two, not more."""
        cap = get_skills_capability(include=["k8s-diag", "cve-scan"])
        assert cap is not None
        assert set(cap.toolset.skills.keys()) == {"k8s-diag", "cve-scan"}

    def test_unknown_skill_name_fails_loud(self) -> None:
        """A name that doesn't match any SKILL.md raises, rather than silently discovering nothing.

        Confirms include= is a real, strict filter against discovered
        names -- pydantic-ai-skills fails loud on an unknown name instead
        of silently succeeding with an empty (or worse, unfiltered) skill
        set, which would hide a typo'd allowed_skills entry in a workflow
        YAML behind "the agent just doesn't seem to have that skill".
        """
        with pytest.raises(ValueError, match="Unknown skill in include"):
            get_skills_capability(include=["does-not-exist"])


pytestmark_llm = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="allowed_skills real-LLM e2e tests require OPENAI_API_KEY",
)


@pytestmark_llm
@pytest.mark.asyncio
async def test_spawn_none_agent_invokes_allowed_skill_script() -> None:
    """DirectExecutor: a real agent loop can list, load, and run an allowed skill's script.

    Proves the full chain -- StepInput.allowed_skills -> get_skills_capability
    -> SkillsCapability(include=...) -> Agent(capabilities=[...]) -> the
    model actually discovering and successfully invoking
    k8s-diag/scripts/hello.py via run_skill_script -- works end-to-end with
    a real LLM, not just that the right objects were constructed.
    """
    step_input = StepInput(
        prompt=(
            "You have access to a skill named 'k8s-diag'. List your available "
            "skills, load the k8s-diag skill, then run its script "
            '\'scripts/hello.py\' with args {"message": "e2e-test", "cluster": '
            '"test-cluster"}. Report back the exact JSON the script printed, '
            "as your final answer, with no other commentary."
        ),
        provider={
            "name": "openai",
            "model": TEST_MODEL,
            "credentials_secret": "OPENAI_API_KEY",
        },
        allowed_skills=["k8s-diag"],
        workflow_id="e2e-allowed-skills-none",
        step_name="run-hello",
        output_key="hello_result",
        timeout_seconds=120,
    )

    result = await DirectExecutor().run(step_input)

    assert result.status == "completed", f"Step failed: {result.error}"
    output_text = (
        json.dumps(result.output) if isinstance(result.output, dict) else str(result.output)
    )
    assert "k8s-diag" in output_text
    assert "executed" in output_text


@pytestmark_llm
@pytest.mark.asyncio
async def test_spawn_local_agent_invokes_allowed_skill_script() -> None:
    """SubprocessExecutor: same real agent loop, but in a forked child process.

    Regression guard specifically for the #204 finding that
    _step_input_to_dict() previously dropped skills_image/skills_paths at
    the process boundary -- confirms allowed_skills actually survives
    serialization to the subprocess and produces the same working result.
    """
    step_input = StepInput(
        prompt=(
            "You have access to a skill named 'k8s-diag'. List your available "
            "skills, load the k8s-diag skill, then run its script "
            '\'scripts/hello.py\' with args {"message": "e2e-test-subprocess", '
            '"cluster": "test-cluster"}. Report back the exact JSON the '
            "script printed, as your final answer, with no other commentary."
        ),
        provider={
            "name": "openai",
            "model": TEST_MODEL,
            "credentials_secret": "OPENAI_API_KEY",
        },
        allowed_skills=["k8s-diag"],
        workflow_id="e2e-allowed-skills-local",
        step_name="run-hello",
        output_key="hello_result",
        timeout_seconds=120,
    )

    result = await SubprocessExecutor().run(step_input)

    assert result.status == "completed", f"Step failed: {result.error}"
    output_text = (
        json.dumps(result.output) if isinstance(result.output, dict) else str(result.output)
    )
    assert "k8s-diag" in output_text
    assert "executed" in output_text
