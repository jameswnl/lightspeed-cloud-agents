"""Test that Helm chart produces expected NetworkPolicy manifests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

HELM_CHART = Path(__file__).parents[2] / "deploy" / "helm"


def _helm_available() -> bool:
    """Check if helm CLI is installed."""
    try:
        subprocess.run(["helm", "version", "--short"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _helm_available(), reason="helm CLI not installed")
class TestHelmNetworkPolicy:
    """Tests for Helm chart NetworkPolicy generation."""

    def _template(self, set_values: list[str] | None = None) -> list[dict]:
        """Run helm template and return parsed YAML documents."""
        cmd = ["helm", "template", "test", str(HELM_CHART)]
        for sv in set_values or []:
            cmd.extend(["--set", sv])
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_defaults_produce_egress_networkpolicy(self) -> None:
        """Default values produce egress NetworkPolicy manifests."""
        docs = self._template()
        np_names = [
            d["metadata"]["name"]
            for d in docs
            if d.get("kind") == "NetworkPolicy"
        ]
        assert any("egress" in n for n in np_names), (
            f"No egress NetworkPolicy found in: {np_names}"
        )

    def test_egress_disabled_omits_egress_policy(self) -> None:
        """Setting egress.enabled=false omits egress NetworkPolicy."""
        docs = self._template(["networkPolicy.egress.enabled=false"])
        np_names = [
            d["metadata"]["name"]
            for d in docs
            if d.get("kind") == "NetworkPolicy"
        ]
        assert not any("egress" in n for n in np_names), (
            f"Egress NetworkPolicy should not be present: {np_names}"
        )
