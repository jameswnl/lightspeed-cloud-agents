"""Tests for the Temporal-free step runner module."""

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestRunStep:
    """Tests for the extracted run_step function."""

    @pytest.fixture(autouse=True)
    def reset_circuit_breaker(self) -> None:
        """Reset the module-level circuit breaker between tests."""
        from cloud_agents.workflow.core.step_runner import _circuit_breaker

        _circuit_breaker._providers.clear()

    @pytest.fixture(name="mock_spawner")
    def mock_spawner_fixture(self, mocker: MockerFixture) -> AsyncMock:
        """Create a mock spawner."""
        spawner = mocker.AsyncMock()
        spawner.spawn.return_value = "http://pod-1:8080"
        spawner.wait_ready.return_value = True
        # get_query_ssl_context() is a *sync* method (see AgentSpawner base
        # class) -- must override the AsyncMock default, which would
        # otherwise make it return an unawaited coroutine instead of None.
        spawner.get_query_ssl_context = mocker.Mock(return_value=None)
        return spawner

    @pytest.fixture(name="mock_http_success")
    def mock_http_success_fixture(self, mocker: MockerFixture) -> None:
        """Mock httpx to return a successful agent response."""
        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "output": {"summary": "done"}}

        mock_transcript_response = mocker.MagicMock()
        mock_transcript_response.status_code = 404

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript_response)

        mock_http = mocker.patch("cloud_agents.workflow.core.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

    @pytest.fixture(name="step_input")
    def step_input_fixture(self) -> dict[str, Any]:
        """Standard step input dict."""
        return {
            "step": {
                "name": "diagnose",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
            "workflow_id": "wf-test-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

    @pytest.mark.asyncio
    async def test_run_step_returns_completed(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Successful step returns completed status with output."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "completed"
        assert result["output"]["summary"] == "done"

    @pytest.mark.asyncio
    async def test_run_step_no_temporal_imports(self) -> None:
        """step_runner module has zero temporalio imports."""
        import cloud_agents.workflow.core.step_runner as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
        assert "@activity.defn" not in source

    @pytest.mark.asyncio
    async def test_run_step_spawns_sandbox(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Step runner calls spawner.spawn with correct args."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.core.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)

        mock_spawner.spawn.assert_called_once()
        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs["env"]["LIGHTSPEED_PROVIDER"] == "openai"
        assert call_kwargs["env"]["LIGHTSPEED_MODEL"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_run_step_forwards_allowed_skills_to_spawner(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """A step's allowed_skills list is forwarded to spawner.spawn() (issue #202)."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        step_input["step"]["allowed_skills"] = ["k8s-diag", "git-ops"]

        from cloud_agents.workflow.core.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs.get("allowed_skills") == ["k8s-diag", "git-ops"]

    @pytest.mark.asyncio
    async def test_run_step_no_allowed_skills_forwards_none(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """A step without allowed_skills forwards None -- no skills visible by default."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.core.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs.get("allowed_skills") is None

    @pytest.mark.asyncio
    async def test_run_step_no_spawner_returns_stub(self, step_input: dict[str, Any]) -> None:
        """Without a spawner, returns a stub result."""
        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=None, attempt=1)
        assert result["status"] == "completed"
        assert "diagnose" in result["output"]["summary"]

    @pytest.mark.asyncio
    async def test_run_step_attempt_in_pod_name(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Attempt number affects the pod name (content-hash)."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.core.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)
        pod_name_1 = mock_spawner.spawn.call_args[0][0]

        mock_spawner.reset_mock()
        await run_step(step_input, spawner=mock_spawner, attempt=2)
        pod_name_2 = mock_spawner.spawn.call_args[0][0]

        assert pod_name_1 != pod_name_2

    @pytest.mark.asyncio
    async def test_run_step_destroys_sandbox(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Sandbox is destroyed after step completes."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        from cloud_agents.workflow.core.step_runner import run_step

        await run_step(step_input, spawner=mock_spawner, attempt=1)
        mock_spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_step_http_502_raises(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """HTTP 502 from sandbox raises RuntimeError for retry."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_response = mocker.MagicMock()
        mock_response.status_code = 502

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)

        mock_http = mocker.patch("cloud_agents.workflow.core.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.core.step_runner import run_step

        with pytest.raises(RuntimeError, match="Infrastructure error"):
            await run_step(step_input, spawner=mock_spawner, attempt=1)

    @pytest.mark.asyncio
    async def test_run_step_uses_spawner_ssl_context_for_query(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Query HTTP client trusts the spawner's own TLS CA when it provides one (#194).

        Without this, the query call to a spawner's exposed HTTPS endpoint
        (e.g. OpenShellSpawner's gateway, behind a self-signed CA) falls
        back to httpx's default system trust store and fails with
        CERTIFICATE_VERIFY_FAILED, even though the spawner already knows
        how to build a correct SSL context for exactly this endpoint.
        """
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_ssl_ctx = mocker.Mock(name="spawner-ssl-context")
        mock_spawner.get_query_ssl_context.return_value = mock_ssl_ctx

        mock_http_cls = mocker.patch("cloud_agents.workflow.core.step_runner.httpx.AsyncClient")
        mock_client = mocker.MagicMock()
        mock_response = mocker.MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True, "output": {"summary": "done"}}
        mock_transcript_response = mocker.MagicMock(status_code=404)
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript_response)
        mock_http_cls.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http_cls.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)

        assert result["status"] == "completed"
        # The query-call AsyncClient is constructed once with client_kwargs
        # (mock_http_success's transcript-collection client is a separate
        # patch target in other tests, but here everything routes through
        # this single patched httpx.AsyncClient) -- find the call that
        # received our SSL context.
        verify_values = [call.kwargs.get("verify") for call in mock_http_cls.call_args_list]
        assert mock_ssl_ctx in verify_values

    @pytest.mark.asyncio
    async def test_run_step_no_ssl_context_omits_verify_kwarg(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """No spawner SSL context, no app-level TLS mode -> no verify override.

        Preserves existing behavior (httpx's own default) for spawners
        that don't need special TLS handling.
        """
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        assert mock_spawner.get_query_ssl_context.return_value is None

        mock_http_cls = mocker.patch("cloud_agents.workflow.core.step_runner.httpx.AsyncClient")
        mock_client = mocker.MagicMock()
        mock_response = mocker.MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True, "output": {"summary": "done"}}
        mock_transcript_response = mocker.MagicMock(status_code=404)
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript_response)
        mock_http_cls.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http_cls.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)

        assert result["status"] == "completed"
        for call in mock_http_cls.call_args_list:
            assert "verify" not in call.kwargs

    @pytest.mark.asyncio
    async def test_run_step_circuit_breaker_open(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Circuit breaker open returns failed without spawning."""
        mocker.patch(
            "cloud_agents.workflow.core.step_runner._circuit_breaker.is_open",
            return_value=True,
        )

        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "failed"
        assert "Circuit breaker" in result["error"]
        mock_spawner.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_step_not_ready_raises(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Sandbox not becoming ready raises RuntimeError."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_spawner.wait_ready.return_value = False

        from cloud_agents.workflow.core.step_runner import run_step

        with pytest.raises(RuntimeError, match="never became ready"):
            await run_step(step_input, spawner=mock_spawner, attempt=1)

    @pytest.mark.asyncio
    async def test_run_step_agent_failure_returns_failed(
        self,
        mock_spawner: AsyncMock,
        step_input: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Agent returning success=false produces failed status."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error": "agent failed",
        }
        mock_transcript = mocker.MagicMock()
        mock_transcript.status_code = 404

        mock_client = mocker.MagicMock()
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mock_client.get = mocker.AsyncMock(return_value=mock_transcript)

        mock_http = mocker.patch("cloud_agents.workflow.core.step_runner.httpx.AsyncClient")
        mock_http.return_value.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_http.return_value.__aexit__ = mocker.AsyncMock(return_value=False)

        from cloud_agents.workflow.core.step_runner import run_step

        result = await run_step(step_input, spawner=mock_spawner, attempt=1)
        assert result["status"] == "failed"
        assert result["error"] == "agent failed"

    @pytest.mark.asyncio
    async def test_run_step_mcp_secrets_require_allowlist(
        self,
        mock_spawner: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """MCP secrets fail-closed when MCP_ALLOWED_SECRETS is unset."""
        mocker.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-test", "MCP_ALLOWED_SECRETS": ""},
            clear=False,
        )

        from cloud_agents.workflow.core.step_runner import run_step

        input_data = {
            "step": {
                "name": "s1",
                "prompt": "test",
                "output_key": "r1",
                "mcp_servers": ["my-server"],
            },
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
            "mcp_servers": [
                {
                    "name": "my-server",
                    "url": "http://mcp:8080",
                    "secret_headers": {
                        "Authorization": {
                            "secret_name": "my-secret",
                            "key": "token",
                        },
                    },
                },
            ],
        }

        with pytest.raises(ValueError, match="MCP_ALLOWED_SECRETS is not set"):
            await run_step(input_data, spawner=mock_spawner, attempt=1)

    @pytest.mark.asyncio
    async def test_run_step_credential_env_key_normalization(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credential is NOT placed in plain env; Provider injects placeholder (issue #199)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        monkeypatch.delenv("openai-api-key", raising=False)

        from cloud_agents.workflow.core.step_runner import run_step

        input_data = {
            "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "openai-api-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        await run_step(input_data, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        env = call_kwargs["env"]
        assert "OPENAI_API_KEY" not in env
        assert "openai-api-key" not in env
        # credential_secret_name now carries the resolved env var KEY
        # (via resolve_credential_env_key()), not the raw K8s-secret-style
        # credentials_secret string -- see issue #240. OpenShellSpawner's
        # own credential_secret_name.upper().replace("-", "_") re-derivation
        # is idempotent on an already-uppercase key, so behavior is
        # unchanged; only the representation is.
        assert call_kwargs["credential_secret_name"] == "OPENAI_API_KEY"

    @pytest.mark.asyncio
    async def test_run_step_credential_secret_unset_falls_back_to_provider_default(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No credentials_secret configured -- ephemeral spawn still gets a
        credential_secret_name via the provider's default env var, matching
        spawn: none/local's existing fallback behavior (regression test for
        issue #240: previously, an unset credentials_secret meant
        credential_secret_name was always None for spawn: ephemeral, so the
        sandbox spawned with zero LLM credentials and no error until the
        agent's first LLM call failed)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-default")

        from cloud_agents.workflow.core.step_runner import run_step

        input_data = {
            "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                # credentials_secret intentionally omitted
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        await run_step(input_data, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs["credential_secret_name"] == "OPENAI_API_KEY"

    @pytest.mark.asyncio
    async def test_run_step_unresolvable_credentials_secret_still_signals_for_fail_loud(
        self,
        mock_spawner: AsyncMock,
        mock_http_success: None,
        mocker: MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """credentials_secret explicitly set but not resolvable (typo'd secret
        name, no matching env var, no provider default either) -- still
        passes the normalized (unresolvable) key through as
        credential_secret_name, rather than None.

        OpenShellSpawner._do_spawn() raises RuntimeError when a non-None
        credential_secret_name doesn't resolve to a real credential -- that
        fail-loud behavior only fires if this layer forwards a real
        (if unresolvable) key instead of silently substituting None or a
        provider default (reviewed on PR #245)."""
        monkeypatch.delenv("MY_TYPOD_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from cloud_agents.workflow.core.step_runner import run_step

        input_data = {
            "step": {"name": "s1", "prompt": "test", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "my-typod-key",
            },
            "sandbox_image": "sandbox:latest",
            "context": {},
        }

        await run_step(input_data, spawner=mock_spawner, attempt=1)

        call_kwargs = mock_spawner.spawn.call_args[1]
        assert call_kwargs["credential_secret_name"] == "MY_TYPOD_KEY"
