"""E2E test: structured output (output_schema) with a real LLM call.

Regression test for issue #188 bug 1: gpt-4o-mini reliably wraps JSON
in markdown fences when only asked via prompt text (no response_format/
native JSON-schema mode), which then failed json.loads() in
_parse_output() with "LLM returned non-JSON response". A unit test with
a mocked LLM response can't catch this -- it would just assert against
whatever behavior was mocked, not against what gpt-4o-mini actually
returns without response_format forced.

Prerequisites:
  - OPENAI_API_KEY set in environment

Usage:
  OPENAI_API_KEY=sk-... uv run pytest tests/e2e/test_structured_output.py -v
"""

from __future__ import annotations

import os

import pytest

from cloud_agents.workflow.executor.step.base import StepInput
from cloud_agents.workflow.executor.step.direct import DirectExecutor

TEST_MODEL = os.environ.get("TEST_LLM_MODEL", "gpt-4o-mini")

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="Structured output e2e test requires OPENAI_API_KEY",
)


@pytest.mark.asyncio
async def test_spawn_none_structured_output_real_llm() -> None:
    """spawn: none step with output_schema completes with real gpt-4o-mini.

    Before the fix, this reproduced the exact failure from #188: the
    model wraps its JSON answer in a ```json fence, DirectExecutor's
    _parse_output() calls json.loads() on the raw content with no
    fence-stripping, and the step fails with "LLM returned non-JSON
    response but output_schema was requested".
    """
    step_input = StepInput(
        prompt="Is a healthy cluster with 0 pod restarts okay to leave as-is?",
        provider={
            "name": "openai",
            "model": TEST_MODEL,
            "credentials_secret": "OPENAI_API_KEY",
        },
        output_schema={
            "type": "object",
            "properties": {
                "healthy": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["healthy", "reason"],
        },
        workflow_id="e2e-structured-output",
        step_name="triage",
        output_key="triage_result",
    )

    result = await DirectExecutor().run(step_input)

    assert result.status == "completed", f"Step failed: {result.error}"
    assert isinstance(result.output, dict)
    assert isinstance(result.output.get("healthy"), bool)
    assert isinstance(result.output.get("reason"), str)
    assert result.output["reason"]
    assert result.input_tokens > 0
    assert result.output_tokens > 0


@pytest.mark.asyncio
async def test_spawn_none_structured_output_survives_repeated_calls() -> None:
    """Run the same structured-output prompt several times.

    gpt-4o-mini's fencing behavior isn't 100% deterministic per the
    issue report ("reliably wraps" -- not "always"), so a single passing
    call isn't strong evidence the fix works. Repetition makes this test
    meaningful instead of possibly getting lucky once.
    """
    step_input = StepInput(
        prompt="Classify this alert: 'disk usage at 95% on node-3'.",
        provider={
            "name": "openai",
            "model": TEST_MODEL,
            "credentials_secret": "OPENAI_API_KEY",
        },
        output_schema={
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "category": {"type": "string"},
            },
            "required": ["severity", "category"],
        },
        workflow_id="e2e-structured-output-repeat",
        step_name="classify",
        output_key="classification",
    )

    for _ in range(3):
        result = await DirectExecutor().run(step_input)
        assert result.status == "completed", f"Step failed: {result.error}"
        assert result.output["severity"] in ("low", "medium", "high", "critical")
