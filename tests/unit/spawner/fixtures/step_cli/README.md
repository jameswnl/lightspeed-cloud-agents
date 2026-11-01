# Step CLI golden fixtures

Real output of the sandbox step CLI (`python3 -P -m lightspeed_agentic.step`),
used by `TestGoldenFixtures` in `tests/unit/spawner/test_step_protocol.py`.

Each `<scenario>/` holds the exec's raw `stdout.jsonl` and, when the CLI
published one, `result.json`. `manifest.json` records the real exit code and
event types, plus the expected step outcome. Those outcomes were written by hand
in `generate.py` from the CLI spec; they are not computed by `step_protocol`.

## Provenance

- Sandbox: lightspeed-agentic-sandbox `fork/lcs-main` at
  `bca5ad0156a92271cebd47f69245f1cd0a6058b0`, in a throwaway `git worktree`
  with `uv sync --frozen --extra dev`.
- Providers: the sandbox's own test fakes (`tests/step_fakes.py`), which run
  the real `lightspeed_agentic.step.main` with a fake provider. No containers,
  gateway or credentials.
- Requests: built by `cloud_agents.spawner.exec_request.build_step_request`,
  except `rejected_schema` (adds `allowedTools`, which the CLI must reject) and
  `rejected_run_dir` (runDir not created). The runDir is created mode 0700
  under `/tmp/lsrun`, as the exec wrapper will.
- Captured on macOS (`prctl` is a no-op there; it doesn't affect stdout).
- The CLI's stderr isn't captured. The `lsrun-pid` line comes from the runner's
  exec wrapper, not the CLI, so the test feeds a synthetic one.

## Regenerate

```bash
git -C ~/ws/lightspeed-agentic-sandbox worktree add --detach /tmp/lsas bca5ad0
(cd /tmp/lsas && uv sync --frozen --extra dev)
uv run --extra openshell python tests/unit/spawner/fixtures/step_cli/generate.py \
    --sandbox-repo /tmp/lsas
git -C ~/ws/lightspeed-agentic-sandbox worktree remove /tmp/lsas
```

Event timestamps change on each run, nothing else should.
