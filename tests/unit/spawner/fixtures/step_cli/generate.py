"""Regenerate the step CLI golden fixtures from the real sandbox step CLI.

Run from this repo's venv (see README.md):

    uv run python tests/unit/spawner/fixtures/step_cli/generate.py \
        --sandbox-repo /path/to/lightspeed-agentic-sandbox

Each scenario builds its stdin with build_step_request(), creates the runDir the
way the exec wrapper will (mode 0700 under /tmp/lsrun), runs the real
``lightspeed_agentic.step.main`` through the sandbox's ``tests.step_fakes`` fake
providers, and records stdout, result.json and the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cloud_agents.spawner.exec_request import RUN_ROOT, build_step_request

HERE = Path(__file__).resolve().parent

# scenario -> (fake provider, step overrides, request patch, CLI args, mkdir runDir,
#              close stdout, expected outcome). Expected outcomes are written by
#              hand from step-cli.md, not computed by step_protocol.
SCENARIOS = {
    "ok": ("ok", {}, {}, [], True, False, ("completed", "ok", "stdout")),
    "agent_error": ("agent_error", {}, {}, [], True, False, ("failed", "agent_failed", "stdout")),
    "infra_502": ("infra", {}, {}, [], True, False, ("error", "infrastructure_error", "stdout")),
    "nan": ("nan", {}, {}, [], True, False, ("completed", "ok", "stdout")),
    "soft_timeout_502": (
        "hang",
        {"timeout_seconds": 1},
        {},
        [],
        True,
        False,
        ("error", "infrastructure_error", "stdout"),
    ),
    "rejected_schema": (
        "ok",
        {},
        {"allowedTools": ["Bash"]},
        [],
        True,
        False,
        ("failed", "rejected", None),
    ),
    "rejected_run_dir": ("ok", {}, {}, [], False, False, ("failed", "rundir_rejected", None)),
    "stdout_closed": ("ok", {}, {}, [], True, True, ("failed", "result_unverified", "file")),
    "hard_deadline": (
        "stubborn",
        {"timeout_seconds": 1},
        {},
        ["--grace-seconds", "1"],
        True,
        False,
        ("error", "deadline", None),
    ),
}


def _run(sandbox_repo: Path, python: str, name: str, spec: tuple) -> dict:
    scenario, kwargs, patch, cli_args, mkdir, close_stdout, expected = spec
    req = build_step_request(
        {"name": f"golden {name}", "prompt": "Diagnose the issue"},
        workflow_id="golden",
        attempt=1,
        context={"steps": {}},
        **kwargs,
    )
    stdin = req.stdin
    if patch:
        stdin = (json.dumps({**json.loads(stdin), **patch}) + "\n").encode()
    if mkdir:
        os.mkdir(req.run_dir, 0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "LIGHTSPEED_"))}
    env["PYTHONPATH"] = str(sandbox_repo)
    cmd = [python, "-P", "-m", "tests.step_fakes", scenario, *cli_args]
    out_dir = HERE / name
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    try:
        if close_stdout:
            read_end, write_end = os.pipe()
            os.close(read_end)
            proc = subprocess.run(
                cmd,
                input=stdin,
                stdout=write_end,
                stderr=subprocess.PIPE,
                cwd="/",
                env=env,
                timeout=60,
                check=False,
            )
            os.close(write_end)
            stdout = b""
        else:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, cwd="/", env=env, timeout=60, check=False
            )
            stdout = proc.stdout
        (out_dir / "stdout.jsonl").write_bytes(stdout)
        result = Path(req.run_dir, "result.json")
        if result.exists():
            shutil.copyfile(result, out_dir / "result.json")
    finally:
        shutil.rmtree(req.run_dir, ignore_errors=True)
    events = [json.loads(line) for line in stdout.splitlines()]
    status, reason, source = expected
    return {
        "provider": scenario,
        "exit_code": proc.returncode,
        "event_types": [r["event"]["type"] for r in events if r["type"] == "event"],
        "status": status,
        "reason": reason,
        "result_source": source,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-repo", type=Path, required=True)
    parser.add_argument("--sandbox-python", help="default: <sandbox-repo>/.venv/bin/python")
    args = parser.parse_args()
    python = args.sandbox_python or str(args.sandbox_repo / ".venv" / "bin" / "python")
    os.makedirs(RUN_ROOT, mode=0o700, exist_ok=True)
    sha = subprocess.run(
        ["git", "-C", str(args.sandbox_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest = {
        name: _run(args.sandbox_repo, python, name, spec) for name, spec in SCENARIOS.items()
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"captured {len(manifest)} scenarios from sandbox {sha}", file=sys.stderr)


if __name__ == "__main__":
    main()
