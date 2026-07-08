"""Unit tests for AgentSpawner base class."""

import pytest
from pydantic import ValidationError

from cloud_agents.spawner.base import AgentSpawner, SpawnConfig


class MockSpawner(AgentSpawner):
    """Test spawner that doesn't actually create containers."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.spawned = []
        self.destroyed = []
        self.written_files: dict[str, str] = {}

    async def _do_spawn(self, agent_name, image, env, config=None, labels=None, **kwargs):
        self.spawned.append(agent_name)
        return f"http://{agent_name}:8080"

    async def _do_destroy(self, agent_name):
        self.destroyed.append(agent_name)

    async def _do_list_active(self, labels=None):
        return list(self.spawned)

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        raise FileNotFoundError(f"No such file: {path}")

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        self.written_files[f"{agent_name}:{path}"] = content


class FailingSpawner(AgentSpawner):
    """Spawner that always fails."""

    async def _do_spawn(self, agent_name, image, env, config=None, labels=None, **kwargs):
        raise RuntimeError("Spawn failed")

    async def _do_destroy(self, agent_name):
        pass

    async def _do_list_active(self, labels=None):
        return []

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        raise FileNotFoundError(f"No such file: {path}")

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        raise RuntimeError("Write failed")


class TestAgentSpawner:
    """Tests for the base AgentSpawner."""

    @pytest.mark.asyncio
    async def test_spawn_returns_endpoint(self) -> None:
        """Test that spawn returns an endpoint URL."""
        spawner = MockSpawner()
        endpoint = await spawner.spawn("test-agent", "image:latest")
        assert endpoint == "http://test-agent:8080"
        assert "test-agent" in spawner.spawned

    @pytest.mark.asyncio
    async def test_spawn_increments_active_count(self) -> None:
        """Test that spawning increments the active count."""
        spawner = MockSpawner()
        assert spawner.active_count == 0
        await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 1
        await spawner.spawn("a2", "image:latest")
        assert spawner.active_count == 2

    @pytest.mark.asyncio
    async def test_destroy_decrements_active_count(self) -> None:
        """Test that destroying decrements the active count."""
        spawner = MockSpawner()
        await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 1
        await spawner.destroy("a1")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_concurrency_cap_enforced(self) -> None:
        """Test that the concurrency cap prevents over-spawning."""
        spawner = MockSpawner(max_pods=2)
        await spawner.spawn("a1", "image:latest")
        await spawner.spawn("a2", "image:latest")
        with pytest.raises(RuntimeError, match="Concurrency cap"):
            await spawner.spawn("a3", "image:latest")

    @pytest.mark.asyncio
    async def test_failed_spawn_doesnt_leak_count(self) -> None:
        """Test that a failed spawn doesn't increment the active count."""
        spawner = FailingSpawner(max_pods=2)
        with pytest.raises(RuntimeError, match="Spawn failed"):
            await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_destroy_below_zero_safe(self) -> None:
        """Test that destroying when count is 0 doesn't go negative."""
        spawner = MockSpawner()
        await spawner.destroy("nonexistent")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_spawn_with_env(self) -> None:
        """Test spawning with environment variables."""
        spawner = MockSpawner()
        endpoint = await spawner.spawn(
            "test",
            "image:latest",
            env={"OLLAMA_URL": "http://ollama:11434/v1"},
        )
        assert endpoint == "http://test:8080"


class TestWaitReadyTLS:
    """Tests for wait_ready with ca_cert_pem TLS parameter."""

    @pytest.mark.asyncio
    async def test_wait_ready_with_ca_cert_pem_creates_ssl_context(self) -> None:
        """wait_ready with ca_cert_pem creates httpx client with verify=SSLContext."""
        import ssl
        from unittest.mock import AsyncMock, MagicMock, patch

        spawner = MockSpawner()
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        ca_pem = b"-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n"
        mock_ssl_ctx = MagicMock(spec=ssl.SSLContext)

        with (
            patch("cloud_agents.spawner.base.httpx.AsyncClient", mock_client_cls),
            patch("ssl.create_default_context", return_value=mock_ssl_ctx) as mock_ctx,
        ):
            result = await spawner.wait_ready(
                "https://pod:8443",
                timeout=5.0,
                ca_cert_pem=ca_pem,
            )

            assert result is True
            mock_ctx.assert_called_once()
            mock_ssl_ctx.load_verify_locations.assert_called_once_with(cadata=ca_pem.decode())
            init_call = mock_client_cls.call_args
            assert init_call[1].get("verify") is mock_ssl_ctx

    @pytest.mark.asyncio
    async def test_wait_ready_without_ca_cert_pem_no_verify(self) -> None:
        """wait_ready without ca_cert_pem does not set verify on httpx client."""
        from unittest.mock import AsyncMock, MagicMock, patch

        spawner = MockSpawner()
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("cloud_agents.spawner.base.httpx.AsyncClient", mock_client_cls):
            result = await spawner.wait_ready(
                "http://pod:8080",
                timeout=5.0,
            )

            assert result is True
            init_call = mock_client_cls.call_args
            assert "verify" not in init_call[1]


class ReadFileSpawner(AgentSpawner):
    """Spawner with read_file implementation for testing."""

    def __init__(self, file_contents: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._file_contents = file_contents or {}

    async def _do_spawn(self, agent_name, image, env, config=None, labels=None, **kwargs):
        return f"http://{agent_name}:8080"

    async def _do_destroy(self, agent_name):
        pass

    async def _do_list_active(self, labels=None):
        return []

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        key = f"{agent_name}:{path}"
        if key not in self._file_contents:
            raise FileNotFoundError(f"No such file: {path}")
        return self._file_contents[key]

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        self._file_contents[f"{agent_name}:{path}"] = content


class TestReadFile:
    """Tests for AgentSpawner.read_file() method."""

    @pytest.mark.asyncio
    async def test_read_file_returns_content(self) -> None:
        """read_file returns content from the container."""
        spawner = ReadFileSpawner(
            file_contents={"pod-1:/var/log/agent-events.jsonl": '{"ts":"t","type":"tool_call"}\n'}
        )
        content = await spawner.read_file("pod-1", "/var/log/agent-events.jsonl")
        assert "tool_call" in content

    @pytest.mark.asyncio
    async def test_read_file_not_found_raises(self) -> None:
        """read_file raises FileNotFoundError when file doesn't exist."""
        spawner = ReadFileSpawner()
        with pytest.raises(FileNotFoundError):
            await spawner.read_file("pod-1", "/nonexistent")

    @pytest.mark.asyncio
    async def test_read_file_is_abstract_method(self) -> None:
        """read_file on the ABC delegates to _do_read_file."""
        spawner = ReadFileSpawner(file_contents={"agent-1:/tmp/test.txt": "hello"})
        result = await spawner.read_file("agent-1", "/tmp/test.txt")
        assert result == "hello"


class TestWriteFile:
    """Tests for AgentSpawner.write_file() method."""

    @pytest.mark.asyncio
    async def test_write_file_delegates_to_do_write_file(self) -> None:
        """write_file delegates to _do_write_file implementation."""
        spawner = MockSpawner()
        await spawner.write_file("agent-1", "/tmp/test.txt", "hello world")
        assert spawner.written_files["agent-1:/tmp/test.txt"] == "hello world"

    @pytest.mark.asyncio
    async def test_write_file_propagates_errors(self) -> None:
        """write_file propagates errors from _do_write_file."""
        spawner = FailingSpawner()
        with pytest.raises(RuntimeError, match="Write failed"):
            await spawner.write_file("agent-1", "/tmp/test.txt", "content")

    @pytest.mark.asyncio
    async def test_write_file_creates_readable_content(self) -> None:
        """Content written via write_file can be read via read_file."""
        spawner = ReadFileSpawner()
        await spawner.write_file("agent-1", "/tmp/test.txt", "written content")
        result = await spawner.read_file("agent-1", "/tmp/test.txt")
        assert result == "written content"

    @pytest.mark.asyncio
    async def test_write_file_overwrites_existing(self) -> None:
        """write_file overwrites existing file content."""
        spawner = ReadFileSpawner(file_contents={"agent-1:/tmp/test.txt": "old content"})
        await spawner.write_file("agent-1", "/tmp/test.txt", "new content")
        result = await spawner.read_file("agent-1", "/tmp/test.txt")
        assert result == "new content"

    @pytest.mark.asyncio
    async def test_write_file_handles_empty_content(self) -> None:
        """write_file accepts empty string content."""
        spawner = MockSpawner()
        await spawner.write_file("agent-1", "/tmp/empty.txt", "")
        assert spawner.written_files["agent-1:/tmp/empty.txt"] == ""

    @pytest.mark.asyncio
    async def test_write_file_handles_multiline_content(self) -> None:
        """write_file accepts multiline content with special characters."""
        spawner = MockSpawner()
        content = '{"event": "test"}\n{"event": "done"}\n'
        await spawner.write_file("agent-1", "/var/run/messages.jsonl", content)
        assert spawner.written_files["agent-1:/var/run/messages.jsonl"] == content


class FailingDestroySpawner(AgentSpawner):
    """Spawner whose _do_destroy always raises."""

    async def _do_spawn(self, agent_name, image, env, config=None, labels=None, **kwargs):
        return f"http://{agent_name}:8080"

    async def _do_destroy(self, agent_name):
        raise RuntimeError("Destroy failed")

    async def _do_list_active(self, labels=None):
        return []

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        raise FileNotFoundError(f"No such file: {path}")

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        pass


class TestDestroyOnFailure:
    """Tests that _active_count decrements correctly even when _do_destroy fails.

    Design rationale: if _do_destroy raises, the spawner has given up on managing
    that pod. Not decrementing would permanently reduce the concurrency cap.
    The max(0, ...) guard prevents underflow.
    """

    @pytest.mark.asyncio
    async def test_destroy_decrements_count_on_failure(self) -> None:
        """Active count still decrements when _do_destroy raises."""
        spawner = FailingDestroySpawner(max_pods=2)
        await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 1

        with pytest.raises(RuntimeError, match="Destroy failed"):
            await spawner.destroy("a1")

        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_destroy_failure_frees_concurrency_slot(self) -> None:
        """A failed destroy frees the slot so new pods can be spawned."""
        spawner = FailingDestroySpawner(max_pods=1)
        await spawner.spawn("a1", "image:latest")

        with pytest.raises(RuntimeError, match="Destroy failed"):
            await spawner.destroy("a1")

        # Slot should be free for a new spawn
        endpoint = await spawner.spawn("a2", "image:latest")
        assert endpoint == "http://a2:8080"


class TestSpawnConfig:
    """Tests for SpawnConfig resource limit validation."""

    def test_default_values(self) -> None:
        """Default SpawnConfig has reasonable defaults."""
        cfg = SpawnConfig()
        assert cfg.cpu_request == "100m"
        assert cfg.cpu_limit == "500m"
        assert cfg.memory_request == "256Mi"
        assert cfg.memory_limit == "512Mi"
        assert cfg.timeout_seconds == 60

    def test_valid_custom_values(self) -> None:
        """Valid custom values are accepted."""
        cfg = SpawnConfig(
            cpu_request="200m",
            cpu_limit="2",
            memory_request="512Mi",
            memory_limit="2Gi",
            timeout_seconds=120,
        )
        assert cfg.cpu_limit == "2"
        assert cfg.memory_limit == "2Gi"

    def test_timeout_too_low_rejected(self) -> None:
        """Timeout below 5 seconds is rejected."""
        with pytest.raises(ValidationError):
            SpawnConfig(timeout_seconds=2)

    def test_timeout_too_high_rejected(self) -> None:
        """Timeout above 300 seconds is rejected."""
        with pytest.raises(ValidationError):
            SpawnConfig(timeout_seconds=600)

    def test_timeout_boundary_low(self) -> None:
        """Timeout of exactly 5 is accepted."""
        cfg = SpawnConfig(timeout_seconds=5)
        assert cfg.timeout_seconds == 5

    def test_timeout_boundary_high(self) -> None:
        """Timeout of exactly 300 is accepted."""
        cfg = SpawnConfig(timeout_seconds=300)
        assert cfg.timeout_seconds == 300

    def test_cpu_limit_exceeds_max_rejected(self) -> None:
        """CPU limit above 4 cores is rejected."""
        with pytest.raises(ValidationError, match="cpu_limit"):
            SpawnConfig(cpu_limit="8")

    def test_cpu_limit_at_max_accepted(self) -> None:
        """CPU limit of exactly 4 cores is accepted."""
        cfg = SpawnConfig(cpu_limit="4")
        assert cfg.cpu_limit == "4"

    def test_memory_limit_exceeds_max_rejected(self) -> None:
        """Memory limit above 4Gi is rejected."""
        with pytest.raises(ValidationError, match="memory_limit"):
            SpawnConfig(memory_limit="8Gi")

    def test_memory_limit_at_max_accepted(self) -> None:
        """Memory limit of exactly 4Gi is accepted."""
        cfg = SpawnConfig(memory_limit="4Gi")
        assert cfg.memory_limit == "4Gi"

    def test_millicore_cpu_accepted(self) -> None:
        """Millicore CPU values are accepted."""
        cfg = SpawnConfig(cpu_limit="1500m")
        assert cfg.cpu_limit == "1500m"

    def test_millicore_cpu_exceeds_max_rejected(self) -> None:
        """Millicore CPU above 4000m is rejected."""
        with pytest.raises(ValidationError, match="cpu_limit"):
            SpawnConfig(cpu_limit="5000m")
