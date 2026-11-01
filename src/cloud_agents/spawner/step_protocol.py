"""Parse the sandbox step CLI's exec output and map it to a step outcome.

The step CLI (``python3 -P -m lightspeed_agentic.step``, contract in
lightspeed-agentic-sandbox ``.ai/spec/what/step-cli.md``) writes JSONL protocol
lines to stdout: ``{"v":1,"type":"event","event":{ts,type,data}}`` records,
then at most one final ``{"v":1,"type":"result","status_code":..,"body":{..}}``.
It also publishes the result record to ``runDir/result.json``.

The agent's tools run as the CLI's uid, so every stdout line and result.json
is untrusted: parsed strictly (no NaN/Infinity, no duplicate keys, no extra
keys), bounded in size, and validated before use. A same-uid descendant that
``setsid``s out can outlive the CLI and replace files in the agent-owned
runDir, so result.json is trusted less than the result line, which the CLI
writes on a private fd: it is only used when the exit code says the line could
not be delivered (exit 4), and a file-sourced result taints a warm sandbox.

Pure module: no I/O. The exec transport feeds chunks in and reads the result
file when :func:`needs_result_file` says so.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from cloud_agents.workflow.core.models import TranscriptEvent
from cloud_agents.workflow.security.redact import redact_secrets

MAX_EVENT_LINE = 256 * 1024
MAX_RESULT_LINE = 4 * 1024 * 1024
MAX_STDOUT_TOTAL = 64 * 1024 * 1024
MAX_PREPROTOCOL_NOISE = 64 * 1024  # login-shell output before the CLI starts
MAX_TRANSCRIPT_EVENTS = 10_000
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_STDERR_TAIL = 16 * 1024
MAX_JSON_DEPTH = 64  # the CLI's records nest about 4 deep
_MAX_PID_LINE = 32

_PID_LINE = re.compile(rb"lsrun-pid ([1-9][0-9]{0,9})")

# step-cli.md rule 11
EXIT_OK = 0
EXIT_NOT_STARTED = 1
EXIT_REJECTED = 2
EXIT_INTERNAL = 3
EXIT_NO_STDOUT = 4
EXIT_DEADLINE = 124


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Record(_Strict):
    v: Literal[1]

    @field_validator("v", mode="before")
    @classmethod
    def _exact_int(cls, value: Any) -> Any:
        # Literal[1] alone accepts True and 1.0 (they compare equal to 1).
        if type(value) is not int:
            raise ValueError("v must be the integer 1")
        return value


class _Event(_Strict):
    ts: str
    type: str
    data: dict[str, Any]


class EventRecord(_Record):
    type: Literal["event"]
    event: _Event


class _RunResponse(BaseModel):
    """Mirrors the sandbox's ``RunResponse``: the body of every 200 result."""

    model_config = ConfigDict(extra="allow", strict=True)

    success: bool
    summary: str


class ResultRecord(_Record):
    type: Literal["result"]
    status_code: int = Field(ge=100, le=599)
    body: dict[str, Any]

    @model_validator(mode="after")
    def _check_200_body(self) -> ResultRecord:
        if self.status_code == 200:
            _RunResponse.model_validate(self.body)
        return self


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-strict JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {text}")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def _within_depth(obj: Any) -> bool:
    """Whether obj nests at most MAX_JSON_DEPTH deep (checked without recursion)."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, list):
            if depth > MAX_JSON_DEPTH:
                return False
            stack.extend((item, depth + 1) for item in value)
    return True


def _parse_record(line: bytes) -> EventRecord | ResultRecord | None:
    """Parse one protocol line strictly; None if it is not a valid record."""
    try:
        obj = json.loads(
            line.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_no_duplicate_keys,
        )
    except (ValueError, RecursionError):  # includes UnicodeDecodeError, JSONDecodeError
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    if not _within_depth(obj):
        return None
    model = {"event": EventRecord, "result": ResultRecord}.get(obj["type"])
    if model is None:
        return None
    try:
        return model.model_validate(obj)
    except ValidationError:
        return None


class StepProtocolReader:
    """Incrementally parse one step exec's stdout (protocol) and stderr (pid, tail).

    Attributes:
        events: Transcript, in order, bounded to MAX_TRANSCRIPT_EVENTS /
            MAX_TRANSCRIPT_BYTES plus one truncation marker.
        result: The final result record, or None if missing, invalid or discarded.
        pid: CLI pid (== pgid) from the exec wrapper's first stderr line, or None.
            Best effort, for in-sandbox kill commands only: in a warm sandbox the
            login shell's rc files run first and could print a fake line. Stopping
            a step must use the exec handle or destroy the sandbox (step-cli.md
            trust model).
        protocol_errors: Invalid lines after the first valid one (dropped).
        oversized_lines: Event lines over MAX_EVENT_LINE, or any line over
            MAX_RESULT_LINE (dropped).
        violation: Set when a line follows the result; the result is discarded.
        abort_reason: ``output_too_large`` or ``preprotocol_noise``; the caller
            must stop the exec. Parsing stops.
        truncated: stdout ended mid-line.
    """

    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []
        self.result: ResultRecord | None = None
        self.pid: int | None = None
        self.protocol_errors = 0
        self.oversized_lines = 0
        self.violation: str | None = None
        self.abort_reason: str | None = None
        self.truncated = False
        self._buf = bytearray()
        self._skipping = False
        self._total = 0
        self._noise = 0
        self._seen_protocol = False
        self._transcript_bytes = 0
        self._dropped_events = 0
        self._stderr_head = bytearray()
        self._stderr_head_done = False
        self._stderr_tail = bytearray()
        self._stderr_cut = False

    def feed(self, stream: str, data: bytes) -> list[TranscriptEvent]:
        """Consume one exec output chunk; return the valid events it completed."""
        if stream == "stderr":
            self._feed_stderr(data)
            return []
        if stream != "stdout":
            raise ValueError(f"unknown exec stream {stream!r}")
        if self.abort_reason:
            return []
        self._total += len(data)
        if self._total > MAX_STDOUT_TOTAL:
            self._abort("output_too_large")
            return []

        new: list[TranscriptEvent] = []
        *lines, tail = data.split(b"\n")
        for part in lines:
            self._append(part)
            if self._skipping:
                self._skipping = False
                self._invalid(oversized=True)
            else:
                line = bytes(self._buf)
                self._buf.clear()
                if (event := self._line(line)) is not None:
                    new.append(event)
            if self.abort_reason:
                return new
        self._append(tail)
        return new

    def finish(self) -> None:
        """Mark the end of stdout; a pending partial line is truncated output."""
        if self._buf or self._skipping:
            self.truncated = True
            if self.result is not None:
                self._violate("partial line after result")
        self._buf.clear()
        self._skipping = False

    def stderr_tail(self, secret_values: set[str]) -> str:
        """The last MAX_STDERR_TAIL characters of stderr, redacted.

        Redaction runs before the final truncation, on a window twice that size.
        If the window was cut, a prefix as long as the longest secret is dropped
        first, so a secret cut at the window start can't leave a suffix behind.
        """
        text = bytes(self._stderr_tail).decode("utf-8", errors="replace")
        if self._stderr_cut:
            text = text[max(map(len, secret_values), default=0) :]
        return redact_secrets(text, secret_values)[-MAX_STDERR_TAIL:]

    def _append(self, part: bytes) -> None:
        if self._skipping:
            return
        if len(self._buf) + len(part) > MAX_RESULT_LINE:
            self._skipping = True
            self._buf.clear()
        else:
            self._buf += part

    def _abort(self, reason: str) -> None:
        self.abort_reason = reason
        self._buf.clear()

    def _violate(self, reason: str) -> None:
        self.violation = self.violation or reason
        self.result = None

    def _invalid(self, *, oversized: bool = False, size: int = MAX_RESULT_LINE) -> None:
        if self.result is not None or self.violation:
            self._violate("line after result")
        elif not self._seen_protocol:
            self._noise += size + 1
            if self._noise > MAX_PREPROTOCOL_NOISE:
                self._abort("preprotocol_noise")
        elif oversized:
            self.oversized_lines += 1
        else:
            self.protocol_errors += 1

    def _line(self, line: bytes) -> TranscriptEvent | None:
        if self.result is not None or self.violation:
            self._violate("line after result")
            return None
        record = _parse_record(line)
        if record is None:
            self._invalid(size=len(line))
            return None
        self._seen_protocol = True
        if isinstance(record, ResultRecord):
            self.result = record
            return None
        if len(line) > MAX_EVENT_LINE:
            self.oversized_lines += 1
            return None
        try:
            event = TranscriptEvent(**record.event.model_dump())
        except ValidationError:  # event type outside the transcript schema
            self.protocol_errors += 1
            return None
        self._keep(event, len(line))
        return event

    def _keep(self, event: TranscriptEvent, size: int) -> None:
        if self._dropped_events == 0 and (
            len(self.events) < MAX_TRANSCRIPT_EVENTS
            and self._transcript_bytes + size <= MAX_TRANSCRIPT_BYTES
        ):
            self.events.append(event)
            self._transcript_bytes += size
            return
        if self._dropped_events == 0:
            self.events.append(
                TranscriptEvent(ts=event.ts, type="error", data={"message": "transcript truncated"})
            )
        self._dropped_events += 1
        self.events[-1].data["dropped_events"] = self._dropped_events

    def _feed_stderr(self, data: bytes) -> None:
        self._stderr_tail += data
        if len(self._stderr_tail) > 2 * MAX_STDERR_TAIL:
            del self._stderr_tail[: -2 * MAX_STDERR_TAIL]
            self._stderr_cut = True
        if self._stderr_head_done:
            return
        head, newline, _ = data.partition(b"\n")
        self._stderr_head += head[: _MAX_PID_LINE + 1 - len(self._stderr_head)]
        if newline:
            self._stderr_head_done = True
            if match := _PID_LINE.fullmatch(self._stderr_head):
                self.pid = int(match.group(1))
        elif len(self._stderr_head) > _MAX_PID_LINE:
            self._stderr_head_done = True


@dataclass(frozen=True)
class StepOutcome:
    """What the runner does with one step exec.

    ``status``:
        ``completed`` -- shape ``body`` into the step output exactly as the HTTP path does.
        ``failed`` -- return a failed step (not retried); ``body`` is set when the agent
            reported ``success: false``. Also used for deterministic, agent-inducible
            failures (protocol violations, exit 3), which a retry would repeat and which
            must not open the provider circuit breaker for other workflows.
        ``error`` -- raise ``RuntimeError(error)`` so ``run_with_retries`` retries, as for
            an HTTP 502 today.
    ``breaker``: record ``success``/``failure`` on the provider circuit breaker, or nothing.
    ``taint_lease``: the sandbox may be tampered with; a warm lease must not be reused.
        Always set for results read from result.json.
    """

    status: Literal["completed", "failed", "error"]
    reason: str
    error: str | None = None
    body: dict[str, Any] | None = None
    breaker: Literal["success", "failure"] | None = None
    result_source: Literal["stdout", "file", "file_after_transport_loss"] | None = None
    taint_lease: bool = False


def needs_result_file(exit_code: int | None, reader: StepProtocolReader) -> bool:
    """Whether the runner should read ``runDir/result.json`` for this exec.

    Only on exit 4 (published but not delivered) or transport loss (``exit_code
    is None``, after the exec is torn down). Exit 0 promises the result line was
    delivered, and other exits make no promise about the file (step-cli.md rule 9).
    """
    if reader.abort_reason or reader.violation:
        return False
    return exit_code is None or exit_code == EXIT_NO_STDOUT


def map_outcome(
    exit_code: int | None,
    reader: StepProtocolReader,
    result_file: bytes | None = None,
) -> StepOutcome:
    """Map a finished step exec to a step outcome, mirroring the HTTP path.

    Parameters:
        exit_code: The exec's exit code, or None if the stream was lost before one.
        reader: The reader after :meth:`StepProtocolReader.finish`.
        result_file: ``runDir/result.json`` contents (read capped at
            MAX_RESULT_LINE + 1), or None if unread or missing. Ignored unless
            :func:`needs_result_file` is true.
    """
    if reader.abort_reason:
        return StepOutcome(
            "failed", reader.abort_reason, error=f"step output rejected: {reader.abort_reason}"
        )
    if reader.violation:
        return StepOutcome(
            "failed", "protocol_violation", error=f"step protocol violation: {reader.violation}"
        )
    if exit_code == EXIT_OK:
        if reader.result is not None:
            return _from_result(reader.result, "stdout")
        # Exit 0 promises the line was delivered on the CLI's private fd, so a
        # missing one was rejected by our validation (too large, too deep, not
        # strict) or tampered with. Don't fall back to the replaceable file.
        return StepOutcome(
            "failed",
            "protocol_violation",
            error="step protocol violation: exit 0 without a valid result line",
        )
    if needs_result_file(exit_code, reader):
        return _from_file(exit_code, result_file)
    if exit_code == EXIT_REJECTED:
        return _rejected(reader.result)
    if exit_code == EXIT_NOT_STARTED:
        # The CLI never ran: the runDir mkdir failed (possible tampering) or
        # python could not import it. The pid line is printed before both, so it
        # can't tell them apart; treat both as a tainted sandbox.
        return StepOutcome(
            "failed",
            "step_cli_not_started",
            error="step CLI did not start (exit 1)",
            taint_lease=True,
        )
    if exit_code == EXIT_INTERNAL:
        # Deterministic or agent-inducible (a planted result.json makes publication
        # fail), so neither retried nor counted against the provider.
        return StepOutcome("failed", "internal_error", error="step CLI internal error (exit 3)")
    if exit_code == EXIT_DEADLINE:
        return StepOutcome(
            "error", "deadline", error="step CLI hard deadline (exit 124)", breaker="failure"
        )
    if exit_code is not None and (exit_code > 128 or exit_code < 0):
        return StepOutcome("error", "killed", error=f"step CLI killed (exit {exit_code})")
    return StepOutcome(
        "error", "unexpected_exit", error=f"step CLI exited {exit_code}", breaker="failure"
    )


def _from_file(exit_code: int | None, data: bytes | None) -> StepOutcome:
    """Exit 4 or transport loss: use result.json, which always taints the lease."""
    lost = exit_code is None
    source = "file_after_transport_loss" if lost else "file"
    record = None
    if data is not None and len(data) <= MAX_RESULT_LINE:
        record = _parse_record(data)
    if isinstance(record, ResultRecord):
        outcome = replace(_from_result(record, source), taint_lease=True)
        if lost and outcome.status == "completed":
            # With no exit code nothing says the CLI published this file rather
            # than a surviving descendant, so never complete from it. Raise
            # instead: run_with_retries reruns the step in a fresh sandbox.
            return StepOutcome(
                "error",
                "transport_lost",
                error="lost exec stream; success in result.json is not trusted",
                result_source=source,
                taint_lease=True,
            )
        return outcome
    if lost:
        return StepOutcome(
            "error",
            "no_result",
            error="lost exec stream, no result",
            breaker="failure",
            taint_lease=True,
        )
    if data is None:
        return StepOutcome(
            "error",
            "no_result",
            error="step CLI exited 4 but result.json could not be read",
            taint_lease=True,
        )
    # Exit 4 promises a complete result.json; anything else is tampering.
    return StepOutcome(
        "failed",
        "protocol_violation",
        error="step protocol violation: invalid result.json after exit 4",
        taint_lease=True,
    )


def _from_result(record: ResultRecord, source: str) -> StepOutcome:
    body = record.body
    if record.status_code == 200:
        if body["success"]:
            return StepOutcome(
                "completed", "ok", body=body, breaker="success", result_source=source
            )
        return StepOutcome(
            "failed",
            "agent_failed",
            error=body.get("error", "agent returned success=false"),
            body=body,
            breaker="failure",
            result_source=source,
        )
    if record.status_code == 502:
        return StepOutcome(
            "error",
            "infrastructure_error",
            error="Infrastructure error from sandbox (502)",
            breaker="failure",
            result_source=source,
        )
    return StepOutcome(
        "failed",
        "sandbox_error",
        error=f"sandbox returned {record.status_code}: {body.get('error')}",
        body=body,
        breaker="failure",
        result_source=source,
    )


def _rejected(record: ResultRecord | None) -> StepOutcome:
    """Exit 2: the CLI rejected the request before any agent code ran."""
    if record is None:
        return StepOutcome("failed", "rejected", error="step request rejected (exit 2)")
    error = f"step request rejected ({record.status_code}): {record.body.get('error')}"
    # A schema rejection always carries pydantic `detail`; a 422 without it is
    # the runDir check (rule 10), so the sandbox's run root may be tampered with.
    if record.status_code == 422 and "detail" not in record.body:
        return StepOutcome("failed", "rundir_rejected", error=error, taint_lease=True)
    return StepOutcome("failed", "rejected", error=error)
