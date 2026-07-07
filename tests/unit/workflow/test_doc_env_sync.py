"""Validate ARCHITECTURE.md Sandbox Runtime config table stays in sync with code.

Parses the config table from ARCHITECTURE.md, greps temporal_activities.py
and spawner files for all env vars set on sandbox containers, and asserts
every code-level env var appears in the documentation table.

If a new env var is added to the sandbox path and not documented,
this test will catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Paths relative to project root
_PROJECT_ROOT = Path(__file__).parents[3]
_ARCHITECTURE_MD = _PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
_TEMPORAL_ACTIVITIES = (
    _PROJECT_ROOT / "src" / "cloud_agents" / "workflow" / "temporal_activities.py"
)
_PODMAN_SPAWNER = _PROJECT_ROOT / "src" / "cloud_agents" / "spawner" / "podman_spawner.py"
_K8S_SPAWNER = _PROJECT_ROOT / "src" / "cloud_agents" / "spawner" / "kubernetes_spawner.py"


def _extract_config_table_text(arch_md: str) -> str:
    """Extract the Sandbox Runtime config table text from ARCHITECTURE.md.

    Returns:
        The raw text of the config table (from the table header to the last row).

    Raises:
        ValueError: If the config table cannot be found.
    """
    full_match = re.search(
        r"(\| Configuration\s*\|.*?\n\|[-| ]+\n(?:\|.*\n)*)",
        arch_md,
    )
    if not full_match:
        raise ValueError("Could not parse Sandbox Runtime config table in ARCHITECTURE.md")
    return full_match.group(1)


def _extract_documented_env_vars(table_text: str) -> set[str]:
    """Extract all env var names mentioned in the config table.

    Looks for backtick-quoted strings that look like env var names
    (ALL_CAPS_WITH_UNDERSCORES) within the table rows.

    Returns:
        Set of env var names found in the documentation table.
    """
    return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", table_text))


def _extract_code_env_vars_from_activities(source: str) -> set[str]:
    """Extract env vars set on the sandbox env_vars dict in temporal_activities.py.

    Matches patterns like:
        env_vars = {"LIGHTSPEED_PROVIDER": ..., "LIGHTSPEED_MODEL": ...}
        env_vars["LIGHTSPEED_MCP_SERVERS"] = ...
        for deploy_var in ("LIGHTSPEED_PROVIDER_URL", ...):

    Returns:
        Set of env var names that are set on sandbox containers.
    """
    env_vars: set[str] = set()

    # Dict literal initialization: env_vars = {"KEY": ..., "KEY": ...}
    literal_match = re.search(r"env_vars\s*=\s*\{([^}]+)\}", source)
    if literal_match:
        for var in re.findall(r'"([A-Z][A-Z0-9_]+)"(?=\s*:)', literal_match.group(1)):
            env_vars.add(var)

    # Direct assignments: env_vars["VAR_NAME"] = ...
    for match in re.finditer(r'env_vars\["([A-Z][A-Z0-9_]+)"\]', source):
        env_vars.add(match.group(1))

    # Loop-assigned deployment vars: for deploy_var in ("VAR1", "VAR2", ...):
    loop_match = re.search(
        r'for deploy_var in \(\s*((?:"[A-Z_]+",?\s*)+)\)',
        source,
    )
    if loop_match:
        for var in re.findall(r'"([A-Z][A-Z0-9_]+)"', loop_match.group(1)):
            env_vars.add(var)

    return env_vars


def _extract_code_env_vars_from_spawners(*sources: str) -> set[str]:
    """Extract env vars set by spawners on the sandbox env dict.

    Matches patterns like:
        env["SANDBOX_TLS_CERT_PATH"] = ...
        client.V1EnvVar(name="SANDBOX_TLS_CERT_PATH", ...)

    Returns:
        Set of env var names added by spawners.
    """
    env_vars: set[str] = set()
    for source in sources:
        # Direct dict assignment: env["VAR_NAME"] = ...
        for match in re.finditer(r'env\["([A-Z][A-Z0-9_]+)"\]', source):
            env_vars.add(match.group(1))
        # K8s V1EnvVar: client.V1EnvVar(name="VAR_NAME", ...)
        for match in re.finditer(r'V1EnvVar\(\s*name="([A-Z][A-Z0-9_]+)"', source):
            env_vars.add(match.group(1))
    return env_vars


class TestDocEnvVarSync:
    """Ensure ARCHITECTURE.md documents all sandbox env vars from code."""

    @pytest.fixture(name="arch_md_content")
    def arch_md_content_fixture(self) -> str:
        """Read ARCHITECTURE.md content."""
        assert _ARCHITECTURE_MD.exists(), f"Missing {_ARCHITECTURE_MD}"
        return _ARCHITECTURE_MD.read_text()

    @pytest.fixture(name="activities_source")
    def activities_source_fixture(self) -> str:
        """Read temporal_activities.py source."""
        assert _TEMPORAL_ACTIVITIES.exists(), f"Missing {_TEMPORAL_ACTIVITIES}"
        return _TEMPORAL_ACTIVITIES.read_text()

    @pytest.fixture(name="spawner_sources")
    def spawner_sources_fixture(self) -> tuple[str, str]:
        """Read spawner source files."""
        assert _PODMAN_SPAWNER.exists(), f"Missing {_PODMAN_SPAWNER}"
        assert _K8S_SPAWNER.exists(), f"Missing {_K8S_SPAWNER}"
        return _PODMAN_SPAWNER.read_text(), _K8S_SPAWNER.read_text()

    def test_all_activity_env_vars_documented(
        self,
        arch_md_content: str,
        activities_source: str,
    ) -> None:
        """Every env var set in temporal_activities.py must appear in config table."""
        table_text = _extract_config_table_text(arch_md_content)
        documented = _extract_documented_env_vars(table_text)
        code_vars = _extract_code_env_vars_from_activities(activities_source)

        missing = code_vars - documented
        assert not missing, (
            f"Env vars set on sandbox containers in temporal_activities.py "
            f"but missing from ARCHITECTURE.md config table: {sorted(missing)}"
        )

    def test_all_spawner_env_vars_documented(
        self,
        arch_md_content: str,
        spawner_sources: tuple[str, str],
    ) -> None:
        """Every env var set by spawners must appear in config table."""
        table_text = _extract_config_table_text(arch_md_content)
        documented = _extract_documented_env_vars(table_text)
        code_vars = _extract_code_env_vars_from_spawners(*spawner_sources)

        missing = code_vars - documented
        assert not missing, (
            f"Env vars set on sandbox containers by spawners "
            f"but missing from ARCHITECTURE.md config table: {sorted(missing)}"
        )

    def test_combined_env_var_coverage(
        self,
        arch_md_content: str,
        activities_source: str,
        spawner_sources: tuple[str, str],
    ) -> None:
        """Combined check: all env vars from activities + spawners are documented."""
        table_text = _extract_config_table_text(arch_md_content)
        documented = _extract_documented_env_vars(table_text)

        all_code_vars = _extract_code_env_vars_from_activities(activities_source)
        all_code_vars |= _extract_code_env_vars_from_spawners(*spawner_sources)

        missing = all_code_vars - documented
        assert not missing, (
            f"Env vars set on sandbox containers but missing from "
            f"ARCHITECTURE.md config table: {sorted(missing)}"
        )

    def test_config_table_exists(self, arch_md_content: str) -> None:
        """ARCHITECTURE.md must contain a Sandbox Runtime config table."""
        table_text = _extract_config_table_text(arch_md_content)
        assert "Configuration" in table_text
        assert "Purpose" in table_text

    def test_extraction_finds_known_env_vars(
        self, activities_source: str
    ) -> None:
        """Sanity check: extraction must find well-known env vars."""
        code_vars = _extract_code_env_vars_from_activities(activities_source)
        expected_always_present = {
            "LIGHTSPEED_PROVIDER",
            "LIGHTSPEED_MODEL",
        }
        missing = expected_always_present - code_vars
        assert not missing, (
            f"Extraction failed to find known env vars: {sorted(missing)}"
        )
