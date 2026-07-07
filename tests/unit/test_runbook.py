"""Tests that the operational runbook references real code artifacts.

Validates that every metric, endpoint, env var, Makefile target, and source
file mentioned in docs/operations/runbook.md actually exists in the codebase.
This prevents runbook drift -- a stale runbook is worse than no runbook.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
RUNBOOK = REPO_ROOT / "docs" / "operations" / "runbook.md"
DEPLOYMENT_MD = REPO_ROOT / "docs" / "DEPLOYMENT.md"
METRICS_PY = REPO_ROOT / "src" / "cloud_agents" / "workflow" / "temporal_metrics.py"
ENTRYPOINT_PY = REPO_ROOT / "src" / "cloud_agents" / "workflow" / "temporal_entrypoint.py"
MAKEFILE = REPO_ROOT / "Makefile"
HELM_PROMETHEUSRULE = REPO_ROOT / "deploy" / "helm" / "templates" / "prometheusrule.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read a file and return its contents."""
    return path.read_text(encoding="utf-8")


def _extract_backtick_refs(text: str) -> list[str]:
    """Extract all single-backtick-quoted references from markdown text.

    Strips fenced code blocks first to avoid matching inside them.
    """
    # Remove fenced code blocks (```...```)
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    return re.findall(r"`([^`\n]+)`", stripped)


def _get_defined_metrics() -> set[str]:
    """Return set of Prometheus metric names defined in temporal_metrics.py."""
    content = _read(METRICS_PY)
    return set(
        re.findall(
            r"^\s*(ls_\w+)\s*=\s*(?:Counter|Gauge|Histogram|Summary)",
            content,
            re.MULTILINE,
        )
    )


def _get_makefile_targets() -> set[str]:
    """Return set of Makefile targets."""
    content = _read(MAKEFILE)
    return set(re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", content, re.MULTILINE))


# ---------------------------------------------------------------------------
# Fixture: fail all tests if runbook does not yet exist
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _require_runbook():
    """Fail tests if runbook has not been created yet (RED phase)."""
    if not RUNBOOK.exists():
        pytest.fail(f"Runbook not found at {RUNBOOK}")


# ---------------------------------------------------------------------------
# Test: runbook exists and is non-trivial
# ---------------------------------------------------------------------------


class TestRunbookExists:
    """Verify the runbook file exists and has substantial content."""

    def test_runbook_file_exists(self):
        """Runbook must exist at docs/operations/runbook.md."""
        assert RUNBOOK.exists(), f"Missing: {RUNBOOK}"

    def test_runbook_is_not_empty(self):
        """Runbook must have meaningful content (at least 500 chars)."""
        content = _read(RUNBOOK)
        assert len(content) > 500, "Runbook is too short to be useful"

    def test_runbook_has_required_sections(self):
        """Runbook must cover all failure scenarios from the issue spec."""
        content = _read(RUNBOOK)
        required_headings = [
            "Health check",
            "Orphaned sandbox",
            "Workflow stuck",
            "LLM provider",
            "Sandbox spawn",
            "Rate limit",
            "TLS",
            "Alert",
            "Schedule",
        ]
        for heading in required_headings:
            assert heading.lower() in content.lower(), (
                f"Runbook missing required section: '{heading}'"
            )


# ---------------------------------------------------------------------------
# Test: metrics referenced in runbook exist in source code
# ---------------------------------------------------------------------------


class TestRunbookMetricReferences:
    """Every ls_* metric name in the runbook must exist in temporal_metrics.py."""

    def test_all_metrics_exist(self):
        """Metrics mentioned in runbook must be defined in source code."""
        runbook = _read(RUNBOOK)
        defined_metrics = _get_defined_metrics()

        # Find metric-shaped references: ls_*_total or ls_*_seconds
        # Uses word boundary to avoid matching fragments inside other words.
        referenced_metrics = set(
            re.findall(
                r"\bls_[a-z][a-z0-9_]+_total\b|\bls_[a-z][a-z0-9_]+_seconds\b",
                runbook,
            )
        )

        missing = referenced_metrics - defined_metrics
        assert not missing, (
            f"Runbook references metrics not defined in "
            f"{METRICS_PY.name}: {missing}"
        )

    def test_key_metrics_are_mentioned(self):
        """Runbook should mention the most operationally important metrics."""
        runbook = _read(RUNBOOK)
        key_metrics = [
            "ls_workflow_step_runs_total",
            "ls_sandbox_orphans_cleaned_total",
            "ls_sandbox_cleanup_failures_total",
            "ls_rate_limit_rejections_total",
            "ls_sandbox_tls_errors_total",
            "ls_sandbox_timeout_total",
        ]
        for metric in key_metrics:
            assert metric in runbook, (
                f"Runbook should mention key metric: {metric}"
            )


# ---------------------------------------------------------------------------
# Test: endpoints referenced in runbook exist
# ---------------------------------------------------------------------------


class TestRunbookEndpointReferences:
    """API endpoints mentioned in the runbook must exist in the codebase."""

    def test_health_endpoints_exist(self):
        """Health check endpoints referenced in runbook must be defined."""
        runbook = _read(RUNBOOK)
        entrypoint = _read(ENTRYPOINT_PY)

        health_endpoints = ["/healthz", "/livez", "/readyz", "/metrics"]
        for ep in health_endpoints:
            if ep in runbook:
                assert ep in entrypoint, (
                    f"Runbook references {ep} but it's not in "
                    f"{ENTRYPOINT_PY.name}"
                )

    def test_cancel_endpoint_referenced(self):
        """Cancel endpoint should be in runbook (for stuck workflow recovery)."""
        runbook = _read(RUNBOOK)
        assert "/cancel" in runbook or "cancel" in runbook.lower(), (
            "Runbook should describe how to cancel stuck workflows"
        )


# ---------------------------------------------------------------------------
# Test: env vars referenced in runbook exist in source code
# ---------------------------------------------------------------------------


class TestRunbookEnvVarReferences:
    """Environment variables mentioned in runbook must be used in code."""

    def test_env_vars_exist_in_code(self):
        """Env vars in runbook backtick refs must appear in source code."""
        runbook = _read(RUNBOOK)
        backtick_refs = _extract_backtick_refs(runbook)

        # Find env-var-shaped refs (ALL_CAPS_WITH_UNDERSCORES)
        env_var_pattern = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
        runbook_env_vars = {
            ref for ref in backtick_refs if env_var_pattern.match(ref)
        }

        if not runbook_env_vars:
            pytest.skip("No env vars found in runbook")

        # Search the entire src/ tree for these env vars
        src_dir = REPO_ROOT / "src"
        src_content = ""
        for py_file in src_dir.rglob("*.py"):
            src_content += py_file.read_text(encoding="utf-8")

        missing = {var for var in runbook_env_vars if var not in src_content}
        assert not missing, (
            f"Runbook references env vars not found in src/: {missing}"
        )


# ---------------------------------------------------------------------------
# Test: Makefile targets referenced in runbook exist
# ---------------------------------------------------------------------------


class TestRunbookMakefileReferences:
    """make commands in the runbook must reference real Makefile targets."""

    def test_make_targets_exist(self):
        """Every `make <target>` in the runbook must exist in the Makefile."""
        runbook = _read(RUNBOOK)
        available_targets = _get_makefile_targets()

        # Find all `make <target>` references
        referenced_targets = set(
            re.findall(r"make\s+([a-zA-Z][a-zA-Z0-9_-]*)", runbook)
        )

        missing = referenced_targets - available_targets
        assert not missing, (
            f"Runbook references Makefile targets that don't exist: {missing}"
        )


# ---------------------------------------------------------------------------
# Test: source files referenced in runbook exist
# ---------------------------------------------------------------------------


class TestRunbookFileReferences:
    """Source file paths in the runbook must point to real files."""

    def test_source_paths_exist(self):
        """Python source paths in the runbook must exist on disk."""
        runbook = _read(RUNBOOK)

        # Match paths like src/cloud_agents/.../*.py
        py_paths = re.findall(r"(?:src/cloud_agents/\S+\.py)", runbook)

        for path_str in py_paths:
            full_path = REPO_ROOT / path_str
            assert full_path.exists(), (
                f"Runbook references non-existent file: {path_str}"
            )


# ---------------------------------------------------------------------------
# Test: Prometheus alert rules referenced in runbook
# ---------------------------------------------------------------------------


class TestRunbookAlertRuleReferences:
    """Alert names in runbook should match the Helm PrometheusRule template."""

    def test_alert_names_match_helm(self):
        """Alert names mentioned in runbook should exist in prometheusrule.yaml."""
        runbook = _read(RUNBOOK)
        helm_content = _read(HELM_PROMETHEUSRULE)

        # Extract alert names from Helm template
        helm_alerts = set(re.findall(r"alert:\s*(\w+)", helm_content))

        # Extract PascalCase alert names from runbook backtick refs
        runbook_alerts = set(
            re.findall(r"`(\w+(?:High|Failure|Detected|Down))`", runbook)
        )

        if not runbook_alerts:
            pytest.skip("No alert names found in runbook")

        missing = runbook_alerts - helm_alerts
        assert not missing, (
            f"Runbook references alerts not in prometheusrule.yaml: {missing}"
        )


# ---------------------------------------------------------------------------
# Test: DEPLOYMENT.md links to runbook
# ---------------------------------------------------------------------------


class TestDeploymentLinksRunbook:
    """DEPLOYMENT.md must link to the operational runbook."""

    def test_deployment_links_to_runbook(self):
        """DEPLOYMENT.md should contain a link to the runbook."""
        deployment = _read(DEPLOYMENT_MD)
        assert "runbook" in deployment.lower(), (
            "DEPLOYMENT.md should link to the operational runbook"
        )
