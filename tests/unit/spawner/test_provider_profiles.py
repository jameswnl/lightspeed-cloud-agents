"""Unit tests for bundled ProviderProfile definitions (issue #247).

CodeRabbit's review on PR #246 flagged that the bundled openai/anthropic
ProviderProfile endpoints used the `access: "read-write"` preset, which
permits unrestricted POST/PUT/PATCH to any path on api.openai.com /
api.anthropic.com -- far more than the sandboxed agent's LLM client
actually needs. These tests pin the endpoints down to an explicit L7
`rules` allowlist (method + path) instead.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock

# Stub openshell if not installed (CI doesn't install the openshell extra).
# Matches the pattern in test_openshell_spawner.py.
if "openshell" not in sys.modules:
    _mock_openshell = MagicMock()
    sys.modules["openshell"] = _mock_openshell
    sys.modules["openshell._proto"] = _mock_openshell._proto
    sys.modules["openshell._proto.openshell_pb2"] = _mock_openshell._proto.openshell_pb2
    sys.modules["openshell._proto.sandbox_pb2"] = _mock_openshell._proto.sandbox_pb2

from pytest_mock import MockerFixture


@contextmanager
def _real_openshell_modules() -> "Iterator[None]":
    """Temporarily swap in the real `openshell` package over the test-stub MagicMock.

    See the identically-named helper in test_openshell_spawner.py for the
    full rationale -- duplicated here rather than imported since it's a
    module-private helper there.
    """
    was_mocked = isinstance(sys.modules.get("openshell"), MagicMock)
    if not was_mocked:
        yield
        return
    snapshot = {
        k: v for k, v in sys.modules.items() if k == "openshell" or k.startswith("openshell.")
    }
    for k in list(snapshot):
        del sys.modules[k]
    try:
        yield
    finally:
        for k in [k for k in sys.modules if k == "openshell" or k.startswith("openshell.")]:
            del sys.modules[k]
        sys.modules.update(snapshot)


class TestBundledProviderProfilesNetworkEndpoints:
    """The bundled openai/anthropic profiles must use explicit L7 rules, not access=read-write."""

    def test_openai_endpoint_uses_explicit_l7_rules_not_read_write(
        self, mocker: MockerFixture
    ) -> None:
        from cloud_agents.spawner.provider_profiles import bundled_provider_profiles

        mock_l7_allow_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Allow")
        mock_l7_rule_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Rule")
        mock_endpoint_cls = mocker.patch("openshell._proto.sandbox_pb2.NetworkEndpoint")

        bundled_provider_profiles()

        mock_l7_allow_cls.assert_any_call(method="POST", path="/v1/chat/completions")
        mock_l7_allow_cls.assert_any_call(method="POST", path="/v1/responses")

        openai_endpoint_call = next(
            call
            for call in mock_endpoint_cls.call_args_list
            if call.kwargs.get("host") == "api.openai.com"
        )
        assert "access" not in openai_endpoint_call.kwargs
        assert openai_endpoint_call.kwargs["protocol"] == "rest"
        assert openai_endpoint_call.kwargs["enforcement"] == "enforce"
        assert openai_endpoint_call.kwargs["rules"] == [mock_l7_rule_cls.return_value] * 2

    def test_anthropic_endpoint_uses_explicit_l7_rules_not_read_write(
        self, mocker: MockerFixture
    ) -> None:
        from cloud_agents.spawner.provider_profiles import bundled_provider_profiles

        mock_l7_allow_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Allow")
        mock_l7_rule_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Rule")
        mock_endpoint_cls = mocker.patch("openshell._proto.sandbox_pb2.NetworkEndpoint")

        bundled_provider_profiles()

        mock_l7_allow_cls.assert_any_call(method="POST", path="/v1/messages")

        anthropic_endpoint_call = next(
            call
            for call in mock_endpoint_cls.call_args_list
            if call.kwargs.get("host") == "api.anthropic.com"
        )
        assert "access" not in anthropic_endpoint_call.kwargs
        assert anthropic_endpoint_call.kwargs["protocol"] == "rest"
        assert anthropic_endpoint_call.kwargs["enforcement"] == "enforce"
        assert anthropic_endpoint_call.kwargs["rules"] == [mock_l7_rule_cls.return_value]

    def test_l7_rules_wrap_l7_allow_instances(self, mocker: MockerFixture) -> None:
        """Each L7Rule must be constructed with `allow=` set to the matching L7Allow."""
        from cloud_agents.spawner.provider_profiles import bundled_provider_profiles

        mock_l7_allow_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Allow")
        mock_l7_rule_cls = mocker.patch("openshell._proto.sandbox_pb2.L7Rule")
        mocker.patch("openshell._proto.sandbox_pb2.NetworkEndpoint")

        bundled_provider_profiles()

        for call in mock_l7_rule_cls.call_args_list:
            assert call.kwargs["allow"] == mock_l7_allow_cls.return_value

    def test_real_protobuf_construction_round_trips(self) -> None:
        """End-to-end sanity check against the real openshell protobuf classes.

        Skipped if the `openshell` extra isn't installed (CI doesn't install
        it) -- the mocked tests above already cover the call-shape contract
        in that environment.
        """
        import importlib

        import pytest

        with _real_openshell_modules():
            try:
                real_sandbox_pb2 = importlib.import_module("openshell._proto.sandbox_pb2")
            except ImportError:
                pytest.skip("openshell extra not installed")
            if isinstance(real_sandbox_pb2, MagicMock):
                pytest.skip("openshell extra not installed (stubbed in sys.modules)")

            # bundled_provider_profiles() must be (re-)imported inside the
            # swap so its lazy `from openshell._proto import ...` resolves
            # the real modules, not a cached reference to the mocks.
            import cloud_agents.spawner.provider_profiles as provider_profiles_module

            profiles = provider_profiles_module.bundled_provider_profiles()

            openai_endpoint = profiles["openai"].endpoints[0]
            assert openai_endpoint.access == ""
            assert openai_endpoint.protocol == "rest"
            assert openai_endpoint.enforcement == "enforce"
            openai_allows = {(r.allow.method, r.allow.path) for r in openai_endpoint.rules}
            assert openai_allows == {("POST", "/v1/chat/completions"), ("POST", "/v1/responses")}

            anthropic_endpoint = profiles["anthropic"].endpoints[0]
            assert anthropic_endpoint.access == ""
            anthropic_allows = {(r.allow.method, r.allow.path) for r in anthropic_endpoint.rules}
            assert anthropic_allows == {("POST", "/v1/messages")}
