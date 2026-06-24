"""Unit tests for parallel step grouping and validation."""

from __future__ import annotations

from agents.workflow.definition import WorkflowStepSpec
from agents.workflow.parallel import group_steps, validate_parallel_groups


def _step(name: str, group: str | None = None, stype: str = "agent") -> WorkflowStepSpec:
    """Create a test step."""
    return WorkflowStepSpec(
        name=name, type=stype, agent="diag" if stype == "agent" else None,
        prompt="test", output_key=name, parallel_group=group,
    )


class TestGroupSteps:
    """Tests for group_steps."""

    def test_all_sequential(self) -> None:
        """Test that steps without parallel_group are individual batches."""
        steps = [_step("a"), _step("b"), _step("c")]
        batches = group_steps(steps)
        assert len(batches) == 3
        assert all(len(b) == 1 for b in batches)

    def test_parallel_group(self) -> None:
        """Test that same-group steps are batched together."""
        steps = [_step("a", "g1"), _step("b", "g1"), _step("c")]
        batches = group_steps(steps)
        assert len(batches) == 2
        assert len(batches[0]) == 2
        assert len(batches[1]) == 1

    def test_mixed_groups(self) -> None:
        """Test sequential → parallel → sequential pattern."""
        steps = [_step("a"), _step("b", "g1"), _step("c", "g1"), _step("d")]
        batches = group_steps(steps)
        assert len(batches) == 3
        assert len(batches[0]) == 1
        assert len(batches[1]) == 2
        assert len(batches[2]) == 1

    def test_multiple_groups(self) -> None:
        """Test two different parallel groups."""
        steps = [_step("a", "g1"), _step("b", "g1"), _step("c", "g2"), _step("d", "g2")]
        batches = group_steps(steps)
        assert len(batches) == 2
        assert len(batches[0]) == 2
        assert len(batches[1]) == 2

    def test_empty_steps(self) -> None:
        """Test empty step list."""
        assert group_steps([]) == []


class TestValidateParallelGroups:
    """Tests for validate_parallel_groups."""

    def test_valid_groups(self) -> None:
        """Test that valid parallel groups pass validation."""
        steps = [_step("a", "g1"), _step("b", "g1")]
        errors = validate_parallel_groups(steps)
        assert errors == []

    def test_approval_in_group_errors(self) -> None:
        """Test that approval steps in groups produce errors."""
        steps = [
            _step("a", "g1"),
            _step("approve", "g1", stype="human-approval"),
        ]
        errors = validate_parallel_groups(steps)
        assert len(errors) == 1
        assert "human-approval" in errors[0]

    def test_no_groups_is_valid(self) -> None:
        """Test that no parallel groups is valid."""
        steps = [_step("a"), _step("b")]
        errors = validate_parallel_groups(steps)
        assert errors == []
