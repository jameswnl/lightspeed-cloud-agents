"""Unit tests for the step CLI stdin request builder."""

import json
import logging
import random
import re

import pytest

from cloud_agents.spawner.exec_request import (
    GATEWAY_MAX_MESSAGE_BYTES,
    MAX_STEP_REQUEST_BYTES,
    check_exec_message_size,
    MAX_TIMEOUT_MS,
    RUN_ROOT,
    StepRequestError,
    StepRequestTooLarge,
    build_step_request,
    derive_step_id,
    new_run_dir,
)

# lightspeed-agentic-sandbox routes/models.py StepRequest
STEP_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
STEP_REQUEST_FIELDS = {
    "query",
    "systemPrompt",
    "outputSchema",
    "context",
    "timeout_ms",
    "stepId",
    "runDir",
    "traceparent",
}


def _names():
    rng = random.Random(1234)
    alphabet = "aZ09 -_./:\\\n\t\x00éß☃İﬃ𝔘​\ud800"
    names = ["", "-", "---", "a" * 1000, "İSTANBUL", "ﬃ", "..", "/etc/passwd", "Ünïcödé step"]
    names += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80))) for _ in range(500)]
    return names


def _build(step=None, **kwargs):
    kwargs.setdefault("workflow_id", "wf-1")
    kwargs.setdefault("attempt", 1)
    kwargs.setdefault("context", {"steps": {}})
    return build_step_request(step if step is not None else {"name": "s", "prompt": "p"}, **kwargs)


def _payload(req):
    assert req.stdin.endswith(b"\n") and req.stdin.count(b"\n") == 1
    return json.loads(req.stdin)


class TestStepId:
    def test_step_id_matches_cli_pattern_for_arbitrary_names(self):
        for name in _names():
            for attempt in (1, 2, 99):
                step_id = derive_step_id("wf-ü:1", name, attempt)
                assert STEP_ID_RE.fullmatch(step_id), (name, step_id)

    def test_deterministic(self):
        assert derive_step_id("wf", "Gather Logs", 1) == derive_step_id("wf", "Gather Logs", 1)

    def test_readable_prefix(self):
        assert derive_step_id("wf", "Gather Logs!", 1).startswith("gather-logs-")

    def test_collision_resistant(self):
        ids = {derive_step_id("wf", name, 1) for name in _names()}
        assert len(ids) == len(set(_names()))
        # Same slug, different names / workflows / attempts.
        variants = [
            ("wf", "a b", 1),
            ("wf", "a_b", 1),
            ("wf", "A B", 1),
            ("wf2", "a b", 1),
            ("wf", "a b", 2),
            ("wf:a", "b", 1),
            ("wf", "a:b", 1),
        ]
        assert len({derive_step_id(*v) for v in variants}) == len(variants)


class TestRunDir:
    def test_direct_child_of_run_root(self):
        run_dir = new_run_dir("step-abc")

        parent, _, name = run_dir.rpartition("/")
        assert parent == RUN_ROOT == "/tmp/lsrun"
        assert re.fullmatch(r"step-abc-[0-9a-f]{16}", name)

    def test_fresh_each_call(self):
        assert new_run_dir("s") != new_run_dir("s")

    def test_custom_run_root(self):
        assert new_run_dir("s", "/var/tmp/x").startswith("/var/tmp/x/s-")

    @pytest.mark.parametrize(
        "root", ["relative", "/tmp/lsrun/", "/tmp//lsrun", "/tmp/../lsrun", ""]
    )
    def test_rejects_non_normalized_root(self, root):
        with pytest.raises(ValueError):
            new_run_dir("s", root)

    def test_rejects_invalid_step_id(self):
        with pytest.raises(ValueError):
            new_run_dir("../x")


class TestBuildStepRequest:
    def test_same_fields_as_http_run(self):
        step = {
            "name": "Diagnose",
            "prompt": "Find the root cause",
            "output_schema": {"type": "object"},
        }
        req = _build(step, context={"steps": {"a": 1}}, system_prompt="be careful")
        payload = _payload(req)

        assert payload["query"] == "Find the root cause"
        assert payload["context"] == {"steps": {"a": 1}}
        assert payload["systemPrompt"] == "be careful"
        assert payload["outputSchema"] == {"type": "object"}
        assert payload["stepId"] == req.step_id == derive_step_id("wf-1", "Diagnose", 1)
        assert payload["runDir"] == req.run_dir
        assert req.run_dir.startswith(f"{RUN_ROOT}/{req.step_id}-")
        assert set(payload) <= STEP_REQUEST_FIELDS

    def test_optional_fields_omitted(self):
        payload = _payload(_build({"name": "s"}))

        assert payload["query"] == ""
        for key in ("systemPrompt", "outputSchema", "timeout_ms", "traceparent"):
            assert key not in payload

    def test_timeout_ms(self):
        assert _payload(_build(timeout_seconds=90))["timeout_ms"] == 90_000
        assert _payload(_build(timeout_seconds=MAX_TIMEOUT_MS // 1000))["timeout_ms"] == (
            MAX_TIMEOUT_MS
        )

    @pytest.mark.parametrize("seconds", [0, -1, MAX_TIMEOUT_MS // 1000 + 1])
    def test_timeout_out_of_range(self, seconds):
        with pytest.raises(StepRequestError, match="timeout"):
            _build(timeout_seconds=seconds)

    def test_allowed_tools_omitted_with_warning(self, caplog):
        step = {
            "name": "s",
            "prompt": "p",
            "permissions": {"allowed_tools": ["Bash"], "denied_tools": ["Write"]},
        }
        with caplog.at_level(logging.WARNING, logger="cloud_agents.spawner.exec_request"):
            payload = _payload(_build(step))

        assert not {"allowedTools", "deniedTools", "allowed_tools", "denied_tools"} & set(payload)
        assert "not enforced" in caplog.text
        assert "'s'" in caplog.text

    def test_no_warning_without_tools(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cloud_agents.spawner.exec_request"):
            _build({"name": "s", "permissions": {"timeout_seconds": 5}})

        assert caplog.text == ""

    def test_includes_traceparent(self, monkeypatch):
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

        def fake_inject(headers):
            headers.update({"traceparent": tp, "tracestate": "k=v"})
            return headers

        monkeypatch.setattr("cloud_agents.spawner.exec_request.inject_traceparent", fake_inject)
        payload = _payload(_build())

        assert payload["traceparent"] == tp
        assert "tracestate" not in payload

    def test_unicode_sent_as_utf8(self):
        req = _build({"name": "s", "prompt": "naïve ☃"})

        assert "naïve ☃".encode() in req.stdin
        assert _payload(req)["query"] == "naïve ☃"

    def test_max_size_accepted(self):
        overhead = len(_build({"name": "s", "prompt": ""}).stdin)
        req = _build({"name": "s", "prompt": "x" * (MAX_STEP_REQUEST_BYTES - overhead)})

        assert len(req.stdin) == MAX_STEP_REQUEST_BYTES

    def test_too_large_raises_before_exec(self):
        overhead = len(_build({"name": "s", "prompt": ""}).stdin)
        with pytest.raises(StepRequestTooLarge, match=str(MAX_STEP_REQUEST_BYTES)) as exc:
            _build({"name": "s", "prompt": "x" * (MAX_STEP_REQUEST_BYTES - overhead + 1)})

        assert isinstance(exc.value, StepRequestError)
        assert "'s'" in str(exc.value)

    def test_cap_fits_cli_and_gateway(self):
        # CLI reads at most 1 MiB including the newline; the gateway's 1 MiB gRPC
        # message also carries env and argv.
        assert MAX_STEP_REQUEST_BYTES <= 1024 * 1024 - 64 * 1024

    def test_too_large_counts_utf8_bytes(self):
        overhead = len(_build({"name": "s", "prompt": ""}).stdin)
        with pytest.raises(StepRequestTooLarge):
            _build({"name": "s", "prompt": "☃" * ((MAX_STEP_REQUEST_BYTES - overhead) // 3 + 1)})

    def test_nan_in_context_rejected(self):
        with pytest.raises(StepRequestError, match="JSON"):
            _build(context={"x": float("nan")})

    def test_lone_surrogate_rejected(self):
        with pytest.raises(StepRequestError, match="JSON"):
            _build({"name": "s", "prompt": "bad \ud800"})

    def test_mcp_without_plain_headers_ok(self):
        servers = [
            {"name": "k8s", "url": "http://mcp", "headers": {}},
            {
                "name": "gh",
                "url": "http://gh",
                "secret_headers": {"Authorization": {"secret_name": "s", "key": "k"}},
            },
        ]
        _payload(_build(mcp_servers=servers))

    def test_mcp_empty_header_name_rejected(self):
        servers = [{"name": "gh", "url": "http://gh", "headers": {"": "SECRET"}}]
        with pytest.raises(StepRequestError):
            _build(mcp_servers=servers)

    def test_mcp_plain_headers_rejected(self):
        servers = [{"name": "gh", "url": "http://gh", "headers": {"Authorization": "tok-123"}}]
        with pytest.raises(StepRequestError, match="gh") as exc:
            _build(mcp_servers=servers)

        assert "tok-123" not in str(exc.value)

    def test_request_contains_no_mcp_config(self):
        servers = [{"name": "k8s", "url": "http://mcp-sentinel"}]
        req = _build(mcp_servers=servers)

        assert b"mcp-sentinel" not in req.stdin


class TestExecMessageSize:
    def test_fits(self):
        check_exec_message_size(b"x" * 1000, {"A": "b"}, ["sh", "-c", "true"])

    def test_env_counts_toward_gateway_cap(self):
        stdin = b"x" * (MAX_STEP_REQUEST_BYTES - 10)
        env = {"LIGHTSPEED_MCP_SERVERS": "y" * (GATEWAY_MAX_MESSAGE_BYTES - MAX_STEP_REQUEST_BYTES)}
        check_exec_message_size(stdin, {}, [])
        with pytest.raises(StepRequestTooLarge, match="env"):
            check_exec_message_size(stdin, env, [])

    def test_argv_counts(self):
        with pytest.raises(StepRequestTooLarge):
            check_exec_message_size(b"", {}, ["a" * GATEWAY_MAX_MESSAGE_BYTES])
