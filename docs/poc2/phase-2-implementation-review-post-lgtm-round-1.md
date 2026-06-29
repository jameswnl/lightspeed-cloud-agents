# Review: Phase 2 post-LGTM range (`75beb29..1eee1ec`)

## Findings

### 1. Major: the new "E2E" suite still validates stub activities, not the real sandbox execution path
The new tests in `tests/e2e/temporal/test_temporal_e2e.py` do connect to a real Temporal server, but every workflow worker is started with `ALL_ACTIVITIES = [run_sandbox_step, build_escalation_activity, send_approval_notification]` and no bound spawner. In the implementation, `run_sandbox_step()` explicitly returns a stub success result when `spawner is None`, so these tests never validate the actual spawn -> readiness -> `/v1/agent/run` -> destroy path. That means the commit message’s "9 tests on real Temporal Server" is true only for the Temporal engine, not for the end-to-end workflow runtime behavior implied by the test names and setup scripts.

Recommended fix: either bind a real/fake spawner that exercises the real activity path and assert spawn/destroy behavior, or rename/scope these tests explicitly as Temporal-server integration tests rather than end-to-end sandbox tests.

### 2. Medium: `setup-kind.sh --run` leaks the background port-forward when tests fail
The setup script uses `set -euo pipefail`, then starts `kubectl port-forward ... &` and runs `uv run pytest tests/e2e/temporal/ -v`. If pytest fails, `set -e` exits the script before `TEST_EXIT=$?` and `kill $PF_PID` run, leaving the background port-forward behind. That makes the failure path messy and can interfere with the next run.

Recommended fix: use a shell `trap` to always kill the background port-forward on exit, or temporarily disable `set -e` around the pytest invocation so cleanup runs reliably.

## Perspective Check
- Functionality: covered; found a significant mismatch between the claimed E2E behavior and what the tests actually execute.
- Quality: covered; setup helpers are useful, but the failure-path cleanup in `setup-kind.sh` is incomplete and the test naming/scope is overstated.
- Security: no major new issues found in this post-LGTM range.

## Verification
- `git status --short`
- `git log -6 --stat --decorate`
- `git diff --name-only bc31ceaae818b326065fbfe7e576b5d863c0329e..HEAD`
- Read: `tests/unit/agents/workflow/temporal/test_activities.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/e2e/temporal/test_temporal_e2e.py`, `tests/e2e/temporal/setup-kind.sh`, `tests/e2e/temporal/teardown-kind.sh`
- `uv run pytest tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_api.py tests/e2e/temporal/test_temporal_e2e.py -q` -> unit tests passed, 9 E2E tests failed locally with `Connection refused` to `localhost:7233` because no Temporal server was running

## Summary
The latest commits add useful regression coverage and convenience scripts, but the strongest issue is scope drift: the new "E2E" suite still runs against stubbed sandbox activity behavior rather than the real sandbox execution path. I did not find a new security-specific regression in this range.
