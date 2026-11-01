"""Unit tests for the step CLI exec protocol reader and outcome mapping.

Every stdout line and result.json is untrusted (step-cli.md trust model), so
most tests here are adversarial.
"""

import json
from pathlib import Path

import pytest

from cloud_agents.workflow.core.execution import is_transient_failure
from cloud_agents.spawner.step_protocol import (
    MAX_EVENT_LINE,
    MAX_PREPROTOCOL_NOISE,
    MAX_RESULT_LINE,
    MAX_STDERR_TAIL,
    MAX_STDOUT_TOTAL,
    MAX_TRANSCRIPT_EVENTS,
    StepProtocolReader,
    map_outcome,
    needs_result_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "step_cli"


def _event(type_="tool_call", **data):
    return {"v": 1, "type": "event", "event": {"ts": "t", "type": type_, "data": data}}


def _result(status_code=200, body=None):
    if body is None:
        body = {"success": True, "summary": "ok"}
    return {"v": 1, "type": "result", "status_code": status_code, "body": body}


def _line(record) -> bytes:
    return json.dumps(record).encode() + b"\n"


def _read(*chunks: bytes, stderr: bytes = b"") -> StepProtocolReader:
    reader = StepProtocolReader()
    if stderr:
        reader.feed("stderr", stderr)
    for chunk in chunks:
        reader.feed("stdout", chunk)
    reader.finish()
    return reader


class TestReader:
    def test_events_then_result(self):
        reader = StepProtocolReader()
        new = reader.feed("stdout", _line(_event(name="Bash")) + _line(_event("tool_result")))
        reader.feed("stdout", _line(_result()))
        reader.finish()

        assert [e.type for e in new] == ["tool_call", "tool_result"]
        assert new[0].data == {"name": "Bash"}
        assert [e.type for e in reader.events] == ["tool_call", "tool_result"]
        assert reader.result.status_code == 200
        assert reader.violation is None and reader.protocol_errors == 0

    def test_utf8_split_across_chunks(self):
        event = json.dumps(_event(input="héllo ☃"), ensure_ascii=False).encode() + b"\n"
        data = event + _line(_result())
        split = data.index("☃".encode()) + 1  # inside the 3-byte sequence
        reader = _read(data[:split], data[split:])

        assert reader.events[0].data == {"input": "héllo ☃"}
        assert reader.result is not None

    def test_every_byte_its_own_chunk(self):
        data = _line(_event()) + _line(_result())
        reader = _read(*(data[i : i + 1] for i in range(len(data))))

        assert len(reader.events) == 1 and reader.result is not None

    @pytest.mark.parametrize(
        "bad_line",
        [
            b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":NaN}}}',
            b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":Infinity}}}',
            b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":-Infinity}}}',
            b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":1e999}}}',
        ],
    )
    def test_rejects_nan(self, bad_line):
        reader = _read(_line(_event()), bad_line + b"\n", _line(_result()))

        assert len(reader.events) == 1
        assert reader.protocol_errors == 1
        assert reader.result is not None

    def test_rejects_nan_in_result(self):
        line = b'{"v":1,"type":"result","status_code":200,"body":{"success":true,"summary":"s","c":NaN}}'
        reader = _read(_line(_event()), line + b"\n")

        assert reader.result is None
        assert reader.protocol_errors == 1

    @pytest.mark.parametrize(
        "record",
        [
            {**_event(), "extra": 1},
            {
                "v": 1,
                "type": "event",
                "event": {"ts": "t", "type": "tool_call", "data": {}, "x": 1},
            },
            {"v": 2, "type": "event", "event": {"ts": "t", "type": "tool_call", "data": {}}},
            {"v": True, "type": "event", "event": {"ts": "t", "type": "tool_call", "data": {}}},
            {"v": 1.0, "type": "event", "event": {"ts": "t", "type": "tool_call", "data": {}}},
            {"v": 1, "type": "event", "event": {"ts": 1, "type": "tool_call", "data": {}}},
            {"v": 1, "type": "event", "event": {"ts": "t", "type": "tool_call", "data": []}},
            {"v": 1, "type": "event", "event": {"ts": "t", "type": "unknown", "data": {}}},
            {"v": 1, "type": "event"},
            {"v": 1, "type": ["event"]},
            {"v": 1, "type": "other"},
            [1, 2],
            "event",
        ],
    )
    def test_invalid_event_records_dropped_and_counted(self, record):
        reader = _read(_line(_event()), _line(record), _line(_result()))

        assert len(reader.events) == 1
        assert reader.protocol_errors == 1
        assert reader.result is not None

    @pytest.mark.parametrize(
        "record",
        [
            {**_result(), "extra": 1},
            _result(status_code="200"),
            _result(status_code=200.0),
            _result(status_code=True),
            _result(status_code=99),
            _result(status_code=600),
            _result(body=[]),
            _result(body={"success": True}),
            _result(body={"summary": "s"}),
            _result(body={"success": "true", "summary": "s"}),
            _result(body={"success": 1, "summary": "s"}),
            _result(body={"success": True, "summary": None}),
        ],
    )
    def test_invalid_result_records_dropped(self, record):
        reader = _read(_line(_event()), _line(record))

        assert reader.result is None
        assert reader.protocol_errors == 1

    def test_200_body_requires_success_and_summary(self):
        ok = _read(_line(_result(body={"success": False, "summary": "s", "rootCause": "oom"})))
        bad = _read(_line(_event()), _line(_result(body={"error": "x"})))

        assert ok.result.body == {"success": False, "summary": "s", "rootCause": "oom"}
        assert bad.result is None

    def test_non_200_body_is_any_object(self):
        reader = _read(_line(_result(502, {"error": "Agent timed out after 1000ms"})))

        assert reader.result.status_code == 502

    def test_duplicate_keys_rejected(self):
        line = b'{"v":1,"type":"result","status_code":502,"status_code":200,"body":{"success":true,"summary":"s"}}'
        reader = _read(_line(_event()), line + b"\n")

        assert reader.result is None
        assert reader.protocol_errors == 1

    def test_moderate_nesting_accepted(self):
        reader = _read(_line(_event(x=[[{"a": [1]}]])), _line(_result()))

        assert reader.events[0].data == {"x": [[{"a": [1]}]]}

    def test_deep_nesting_does_not_crash(self):
        line = b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":' + (
            b"[" * 100_000 + b"]" * 100_000 + b"}}}\n"
        )
        reader = _read(_line(_event()), line, _line(_result()))

        assert len(reader.events) == 1 and reader.result is not None

    def test_huge_integer_does_not_crash(self):
        line = b'{"v":1,"type":"event","event":{"ts":"t","type":"tool_call","data":{"x":%s}}}\n' % (
            b"9" * 10_000
        )
        reader = _read(_line(_event()), line, _line(_result()))

        assert reader.protocol_errors == 1 and reader.result is not None

    def test_non_utf8_line_dropped(self):
        reader = _read(_line(_event()), b'{"v":1,"type":"event","\xff\xfe"}\n', _line(_result()))

        assert reader.protocol_errors == 1
        assert reader.result is not None

    def test_oversize_event_dropped_and_counted(self):
        big = _line(_event(input="x" * MAX_EVENT_LINE))
        assert MAX_EVENT_LINE < len(big) < MAX_RESULT_LINE
        reader = _read(_line(_event()), big, _line(_event("tool_result")), _line(_result()))

        assert [e.type for e in reader.events] == ["tool_call", "tool_result"]
        assert reader.oversized_lines == 1
        assert reader.result is not None

    def test_large_result_line_accepted(self):
        record = _result(body={"success": False, "summary": "x" * (MAX_EVENT_LINE * 4)})
        reader = _read(_line(_event()), _line(record))

        assert reader.result is not None
        assert reader.oversized_lines == 0

    def test_line_over_result_cap_is_not_buffered(self):
        reader = StepProtocolReader()
        reader.feed("stdout", _line(_event()))
        chunk = b"x" * (1024 * 1024)
        for _ in range(MAX_RESULT_LINE // len(chunk) + 2):
            reader.feed("stdout", chunk)
            assert len(reader._buf) <= MAX_RESULT_LINE
        reader.feed("stdout", b"\n" + _line(_result()))
        reader.finish()

        assert reader.oversized_lines == 1
        assert reader.result is not None

    def test_second_result_is_violation(self):
        reader = _read(_line(_result(502, {"error": "x"})), _line(_result()))

        assert reader.violation is not None
        assert reader.result is None

    def test_line_after_result_is_violation(self):
        reader = _read(_line(_event()), _line(_result()), _line(_event()))

        assert reader.violation is not None
        assert reader.result is None
        assert len(reader.events) == 1

    def test_garbage_after_result_is_violation(self):
        reader = _read(_line(_result()), b"junk\n")

        assert reader.violation is not None and reader.result is None

    def test_result_before_events_is_violation(self):
        reader = _read(_line(_result()), _line(_event()), _line(_event("tool_result")))

        assert reader.violation is not None
        assert reader.result is None
        assert reader.events == []

    def test_partial_last_line_is_truncated_not_accepted(self):
        data = _line(_result())
        reader = _read(_line(_event()), data[:-1])  # complete JSON, no newline

        assert reader.result is None
        assert reader.truncated

    def test_truncated_result_line(self):
        data = _line(_result())
        reader = _read(_line(_event()), data[: len(data) // 2])

        assert reader.result is None
        assert reader.truncated
        assert reader.violation is None

    def test_partial_line_after_result_is_violation(self):
        reader = _read(_line(_result()), b'{"v":1')

        assert reader.violation is not None and reader.result is None

    def test_preprotocol_noise_tolerated(self):
        reader = _read(b"Welcome to the login shell\n\n", _line(_event()), _line(_result()))

        assert len(reader.events) == 1
        assert reader.protocol_errors == 0
        assert reader.result is not None
        assert reader.abort_reason is None

    def test_preprotocol_noise_capped_64k(self):
        reader = StepProtocolReader()
        line = b"n" * 1023 + b"\n"
        for _ in range(MAX_PREPROTOCOL_NOISE // len(line)):
            reader.feed("stdout", line)
        assert reader.abort_reason is None
        reader.feed("stdout", line)

        assert reader.abort_reason == "preprotocol_noise"

    def test_preprotocol_oversized_line_aborts(self):
        reader = _read(b"n" * (MAX_RESULT_LINE + 1) + b"\n")

        assert reader.abort_reason == "preprotocol_noise"

    def test_noise_after_protocol_is_protocol_error(self):
        reader = _read(_line(_event()), b"noise\n", _line(_result()))

        assert reader.protocol_errors == 1 and reader.result is not None

    def test_output_too_large_aborts_and_stops_parsing(self):
        reader = StepProtocolReader()
        chunk = _line(_event(input="x" * 1000)) * 1000
        fed = 0
        while fed <= MAX_STDOUT_TOTAL:
            reader.feed("stdout", chunk)
            fed += len(chunk)
        assert reader.abort_reason == "output_too_large"
        assert reader.feed("stdout", _line(_result())) == []
        reader.finish()
        assert reader.result is None

    def test_transcript_bounded_with_truncation_marker(self):
        reader = StepProtocolReader()
        line = _line(_event())
        reader.feed("stdout", line * (MAX_TRANSCRIPT_EVENTS + 50))
        more = reader.feed("stdout", line)
        reader.feed("stdout", _line(_result()))
        reader.finish()

        assert len(more) == 1  # still forwarded for progress
        assert len(reader.events) == MAX_TRANSCRIPT_EVENTS + 1
        marker = reader.events[-1]
        assert marker.type == "error"
        assert marker.data["message"] == "transcript truncated"
        assert marker.data["dropped_events"] == 51

    def test_unknown_stream_rejected(self):
        with pytest.raises(ValueError):
            StepProtocolReader().feed("stdin", b"x")


class TestStderr:
    def test_pid_from_first_stderr_line(self):
        reader = _read(stderr=b"lsrun-pid 4242\nINFO starting\n")

        assert reader.pid == 4242

    def test_pid_line_split_across_chunks(self):
        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-")
        reader.feed("stderr", b"pid 77")
        reader.feed("stderr", b"\nrest")

        assert reader.pid == 77

    def test_pid_only_from_first_stderr_line(self):
        reader = StepProtocolReader()
        reader.feed("stderr", b"INFO noise\n")
        reader.feed("stderr", b"lsrun-pid 4242\n")

        assert reader.pid is None

    def test_later_pid_lines_ignored(self):
        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-pid 10\nlsrun-pid 20\n")
        reader.feed("stderr", b"lsrun-pid 30\n")

        assert reader.pid == 10

    @pytest.mark.parametrize(
        "line",
        [
            b"lsrun-pid 0",
            b"lsrun-pid 012",
            b"lsrun-pid -5",
            b"lsrun-pid 12345678901",
            b"lsrun-pid 12 ",
            b" lsrun-pid 12",
            b"lsrun-pid 12\r",
            b"lsrun-pid \xd9\xa3",  # Arabic-Indic digit three
        ],
    )
    def test_malformed_pid_line(self, line):
        assert _read(stderr=line + b"\n").pid is None

    def test_unterminated_pid_line_not_accepted(self):
        assert _read(stderr=b"lsrun-pid 12").pid is None

    def test_overlong_first_line_without_newline(self):
        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-pid 1" + b"1" * 1000)
        reader.feed("stderr", b"\nlsrun-pid 5\n")

        assert reader.pid is None

    def test_stderr_tail_bounded_and_redacted(self):
        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-pid 1\n" + b"a" * (MAX_STDERR_TAIL * 2))
        reader.feed("stderr", b"\nerror: token sk-secret leaked\n")

        tail = reader.stderr_tail({"sk-secret"})
        assert len(tail) <= MAX_STDERR_TAIL
        assert "sk-secret" not in tail
        assert "***REDACTED***" in tail

    @pytest.mark.parametrize("kept", range(1, 20))
    def test_stderr_tail_secret_straddling_window_is_redacted(self, kept):
        # The last `kept` chars of the secret sit inside a MAX_STDERR_TAIL window.
        secret = "sk-0123456789abcdef"
        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-pid 1\n" + b"x" * 100_000 + secret.encode())
        reader.feed("stderr", b"y" * (MAX_STDERR_TAIL - kept))

        tail = reader.stderr_tail({secret})
        for i in range(1, len(secret) - 3):
            assert secret[i:] not in tail
        assert len(tail) <= MAX_STDERR_TAIL

    def test_stderr_tail_tolerates_split_utf8(self):
        reader = StepProtocolReader()
        reader.feed("stderr", "☃".encode() * MAX_STDERR_TAIL)

        assert isinstance(reader.stderr_tail(set()), str)


def _ok_stdout():
    return _read(_line(_event()), _line(_result()), stderr=b"lsrun-pid 9\n")


def _empty(pid=True):
    return _read(stderr=b"lsrun-pid 9\n" if pid else b"")


FILE_OK = json.dumps(_result(body={"success": True, "summary": "from file"})).encode()


class TestNeedsResultFile:
    @pytest.mark.parametrize(
        ("exit_code", "reader", "expected"),
        [
            (0, _ok_stdout, False),
            (0, _empty, False),
            (4, _empty, True),
            (4, _ok_stdout, True),
            (None, _empty, True),
            (None, _ok_stdout, True),
            (1, _empty, False),
            (2, _empty, False),
            (3, _empty, False),
            (124, _empty, False),
            (137, _empty, False),
            (143, _empty, False),
        ],
    )
    def test_matrix(self, exit_code, reader, expected):
        assert needs_result_file(exit_code, reader()) is expected

    def test_not_after_violation_or_abort(self):
        violated = _read(_line(_result()), _line(_result()))
        assert not needs_result_file(0, violated)
        aborted = _read(b"n" * (MAX_RESULT_LINE + 1) + b"\n")
        assert not needs_result_file(0, aborted)


class TestMapOutcome:
    def test_success_from_stdout(self):
        out = map_outcome(0, _ok_stdout())

        assert out.status == "completed"
        assert out.body == {"success": True, "summary": "ok"}
        assert out.breaker == "success"
        assert out.result_source == "stdout"

    def test_success_false_is_failed(self):
        reader = _read(_line(_result(body={"success": False, "summary": "Agent error: boom"})))
        out = map_outcome(0, reader)

        assert out.status == "failed"
        assert out.error == "agent returned success=false"  # same default as HTTP today
        assert out.breaker == "failure"

    def test_success_false_uses_body_error(self):
        body = {"success": False, "summary": "s", "error": "bad thing"}
        out = map_outcome(0, _read(_line(_result(body=body))))

        assert out.error == "bad thing"
        assert out.body == body

    def test_502_raises_like_http(self):
        out = map_outcome(0, _read(_line(_result(502, {"error": "Infrastructure error: x"}))))

        assert out.status == "error"
        assert out.error == "Infrastructure error from sandbox (502)"
        assert out.breaker == "failure"

    def test_other_status_is_failed(self):
        out = map_outcome(0, _read(_line(_result(400, {"error": "nope"}))))

        assert out.status == "failed"
        assert out.error == "sandbox returned 400: nope"
        assert out.breaker == "failure"

    @pytest.mark.parametrize(
        "stdout",
        [
            b"",
            _line(_result())[:-5],  # truncated
            _line(_result())[:-1],  # complete JSON, no newline
            _line(_result(body={"success": "yes", "summary": "s"})),  # rejected
        ],
    )
    def test_exit0_without_valid_line_is_violation_not_file(self, stdout):
        out = map_outcome(0, _read(_line(_event()), stdout), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "protocol_violation", None)
        assert out.result_source is None

    def test_exit0_too_deep_line_with_forged_file_is_violation(self):
        # The agent makes its own result too deep to accept, then a setsid'd
        # descendant replaces result.json with a forged success.
        deep = {"success": True, "summary": "s", "x": json.loads("[" * 70 + "]" * 70)}
        forged = json.dumps(_result(body={"success": True, "summary": "forged"})).encode()
        reader = _read(_line(_event()), _line(_result(body=deep)))

        assert reader.result is None
        assert not needs_result_file(0, reader)
        out = map_outcome(0, reader, forged)
        assert (out.status, out.reason, out.breaker) == ("failed", "protocol_violation", None)

    def test_exit0_oversized_result_line_is_violation(self):
        record = _result(body={"success": True, "summary": "x" * MAX_RESULT_LINE})
        out = map_outcome(0, _read(_line(_event()), _line(record)), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "protocol_violation", None)

    @pytest.mark.parametrize("exit_code", [4, None])
    @pytest.mark.parametrize(
        "record",
        [
            _result(body={"success": True, "summary": "from file"}),
            _result(body={"success": False, "summary": "s", "error": "bad"}),
            _result(502, {"error": "Infrastructure error: x"}),
            _result(400, {"error": "nope"}),
        ],
    )
    def test_file_result_never_trusted(self, exit_code, record):
        # completed may only come from a valid stdout line; result.json is
        # replaceable by a same-uid descendant, so it is a diagnostic only.
        out = map_outcome(exit_code, _ok_stdout(), json.dumps(record).encode())

        assert (out.status, out.reason, out.breaker) == ("failed", "result_unverified", None)
        assert out.taint_lease
        assert out.body is None
        assert out.result_source == ("file" if exit_code == 4 else "file_after_transport_loss")
        assert str(record["status_code"]) in out.diagnostic
        assert "untrusted" in out.diagnostic
        assert not is_transient_failure(out.error)

    @pytest.mark.parametrize(
        "body",
        [
            # Secrets straddling the old 200-char cut, in error and summary.
            {"success": False, "summary": "s" * 190 + "SECRETabcdefghij", "error": "e"},
            {"success": True, "summary": "ok", "error": "e" * 195 + "SECRETabcdefghij"},
            {"success": True, "summary": "SECRET" + "q" * 500},  # longer than the old limit
            {"success": True, "summary": "☃SECRET☃" * 40},  # multibyte
            {"success": True, "summary": "s", "SECRETkey": 1},  # agent-chosen key
            {"error": "SECRET timed out 502 connection"},
            {"success": True, "summary": "s", "SECRETabcdefghij": "SECRETabcdefghij"},
            {"success": False, "summary": "s", "error": {"SECRET": ["SECRET"]}},
            {"error": ["SECRET"], "summary": {"SECRET": 1}, "detail": 7},
        ],
    )
    def test_file_diagnostic_has_no_file_text(self, body):
        status = 200 if "success" in body else 502
        data = json.dumps(_result(status, body), ensure_ascii=False).encode()
        for exit_code in (4, None):
            out = map_outcome(exit_code, _empty(), data)

            assert out.reason == "result_unverified"
            assert "SECRET" not in out.diagnostic
            assert "☃" not in out.diagnostic and "timed out" not in out.diagnostic
            assert "qqqq" not in out.diagnostic and "ssss" not in out.diagnostic
            assert f"status={status}" in out.diagnostic
            assert f"bytes={len(data)}" in out.diagnostic

    def test_file_diagnostic_metadata(self):
        body = {"success": False, "summary": "s", "error": "e", "rootCause": "x", "other": 1}
        out = map_outcome(4, _empty(), json.dumps(_result(body=body)).encode())

        assert out.diagnostic == (
            "untrusted result.json: status=200 success=False has_error=True has_summary=True "
            f"has_detail=False has_output=False other_keys=2 bytes={len(json.dumps(_result(body=body)))}"
        )

    def test_exit4_unreadable_file_fails(self):
        out = map_outcome(4, _empty(), None)

        assert (out.status, out.reason, out.breaker) == ("failed", "no_result", None)
        assert out.taint_lease

    def test_result_file_with_trailing_newline_parsed(self):
        out = map_outcome(4, _empty(), FILE_OK + b"\n")

        assert out.reason == "result_unverified"

    def test_oversized_result_file_is_protocol_violation(self):
        out = map_outcome(4, _empty(), b" " * MAX_RESULT_LINE + FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "protocol_violation", None)
        assert out.taint_lease

    @pytest.mark.parametrize("data", [None, b"planted"])
    def test_transport_loss_no_result_raises(self, data):
        out = map_outcome(None, _ok_stdout(), data)

        assert out.status == "error"
        assert out.error == "lost exec stream, no result"
        assert out.breaker == "failure"
        assert out.taint_lease

    def test_exit2_not_retryable_no_breaker(self):
        body = {"error": "Invalid request", "detail": [{"type": "extra_forbidden"}]}
        out = map_outcome(2, _read(_line(_result(422, body))))

        assert out.status == "failed"
        assert out.reason == "rejected"
        assert out.error == "step request rejected (422): Invalid request"
        assert out.breaker is None
        assert not out.taint_lease

    def test_exit2_413(self):
        out = map_outcome(2, _read(_line(_result(413, {"error": "Request exceeds 1048576 bytes"}))))

        assert out.status == "failed" and out.error.startswith("step request rejected (413)")

    def test_exit2_rundir_rejection_taints_lease(self):
        body = {"error": "runDir must be an existing directory, not a symlink"}
        out = map_outcome(2, _read(_line(_result(422, body))))

        assert out.status == "failed"
        assert out.reason == "rundir_rejected"
        assert out.taint_lease
        assert out.breaker is None

    def test_exit2_without_line(self):
        out = map_outcome(2, _empty())

        assert out.status == "failed"
        assert out.error == "step request rejected (exit 2)"
        assert out.breaker is None

    def test_exit2_never_reads_result_file(self):
        assert map_outcome(2, _empty(), FILE_OK).status == "failed"

    @pytest.mark.parametrize("pid", [True, False])
    def test_exit1_not_started_taints_lease(self, pid):
        out = map_outcome(1, _empty(pid=pid), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "step_cli_not_started", None)
        assert out.taint_lease

    def test_exit3_fails_without_retry_or_breaker(self):
        # Agent-inducible (for example by planting result.json), so deterministic.
        out = map_outcome(3, _ok_stdout(), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "internal_error", None)
        assert "exit 3" in out.error
        assert out.result_source is None

    def test_exit124_raises(self):
        out = map_outcome(124, _empty(), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("error", "deadline", "failure")
        assert "exit 124" in out.error
        assert out.result_source is None

    @pytest.mark.parametrize("code", [137, 143, 130, -9])
    def test_killed(self, code):
        out = map_outcome(code, _ok_stdout(), FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("error", "killed", None)

    def test_unexpected_exit_code(self):
        out = map_outcome(5, _empty())

        assert (out.status, out.reason, out.breaker) == ("error", "unexpected_exit", "failure")

    def test_violation_fails_without_fallback(self):
        reader = _read(_line(_result()), _line(_result()))
        out = map_outcome(0, reader, FILE_OK)

        assert (out.status, out.reason, out.breaker) == ("failed", "protocol_violation", None)

    @pytest.mark.parametrize(
        ("data", "reason"),
        [
            (b"n" * (MAX_RESULT_LINE + 1) + b"\n", "preprotocol_noise"),
        ],
    )
    def test_abort_fails_regardless_of_exit(self, data, reason):
        for code in (0, 4, None, 137):
            out = map_outcome(code, _read(data), FILE_OK)
            assert (out.status, out.reason, out.breaker) == ("failed", reason, None)


class TestRetryClassification:
    """``error`` outcomes raise, but only the 502 one is transient for run_with_retries.

    The rest fail the step without a rerun, so a step's side effects never repeat.
    """

    def test_502_is_transient(self):
        out = map_outcome(0, _read(_line(_result(502, {"error": "connection refused"}))))

        assert out.status == "error"
        assert is_transient_failure(out.error)

    @pytest.mark.parametrize(
        ("exit_code", "reader", "result_file"),
        [
            (None, _ok_stdout, None),  # lost stream
            (None, _ok_stdout, FILE_OK),  # lost stream, file unverified
            (124, _empty, None),
            (137, _ok_stdout, None),
            (143, _empty, None),
            (-9, _empty, None),
            (5, _empty, None),
            (4, _empty, None),  # unreadable result.json
            (4, _empty, FILE_OK),
            (4, _empty, json.dumps(_result(502, {"error": "timed out"})).encode()),
            (None, _empty, json.dumps(_result(502, {"error": "connection"})).encode()),
            (3, _empty, None),
            (1, _empty, None),
        ],
    )
    def test_other_outcomes_not_transient(self, exit_code, reader, result_file):
        out = map_outcome(exit_code, reader(), result_file)

        assert out.status != "completed"
        assert not is_transient_failure(out.error), out.error

    @pytest.mark.parametrize(
        "stdout",
        [
            b"",
            _line(_result()) + _line(_result()),
            b"n" * (MAX_RESULT_LINE + 1) + b"\n",
        ],
    )
    def test_protocol_failures_not_transient(self, stdout):
        out = map_outcome(0, _read(stdout))

        assert out.status == "failed"
        assert not is_transient_failure(out.error), out.error


def _manifest():
    return json.loads((FIXTURES / "manifest.json").read_text())


def _chunked(data: bytes, size: int):
    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]


class TestGoldenFixtures:
    """Outputs captured from the real sandbox step CLI (see fixtures/step_cli/README.md)."""

    @pytest.mark.parametrize("scenario", sorted(_manifest()))
    @pytest.mark.parametrize("chunk_size", [1, 7, 4096, 1 << 30])
    def test_fixture(self, scenario, chunk_size):
        spec = _manifest()[scenario]
        stdout = (FIXTURES / scenario / "stdout.jsonl").read_bytes()
        result_path = FIXTURES / scenario / "result.json"
        result_file = result_path.read_bytes() if result_path.exists() else None

        reader = StepProtocolReader()
        reader.feed("stderr", b"lsrun-pid 1234\n")
        for chunk in _chunked(stdout, chunk_size):
            reader.feed("stdout", chunk)
        reader.finish()

        assert reader.protocol_errors == 0
        assert reader.oversized_lines == 0
        assert reader.violation is None and reader.abort_reason is None
        assert [e.type for e in reader.events] == spec["event_types"]
        file_arg = result_file if needs_result_file(spec["exit_code"], reader) else None
        out = map_outcome(spec["exit_code"], reader, file_arg)
        assert out.status == spec["status"]
        assert out.reason == spec["reason"]
        assert out.result_source == spec["result_source"]

    def test_fixture_set_covers_every_cli_outcome(self):
        exits = {spec["exit_code"] for spec in _manifest().values()}
        assert {0, 2, 4, 124} <= exits
