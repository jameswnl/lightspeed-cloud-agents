# Review: Phase 2 implementation round 7 (`fd555e0..072f0f2`)

## Findings

### 1. Major: config-ref to env-var mapping still breaks the documented Phase 2 contract for hyphenated refs
This commit exposes `notifier_config` and `escalation_config` on `RunWorkflowRequest` and wires them through to `WorkflowInput`, which fixes the caller-side reachability gap. But the runtime resolution code still derives env var names by uppercasing the raw `config_ref` only. The Phase 2 contract examples use hyphenated refs such as `slack-approval-channel` and `escalation-endpoint`, while the documented env vars are shell-style names like `NOTIFIER_SLACK_<REF>_WEBHOOK_URL` and `ESCALATION_WEBHOOK_<REF>_URL`. With the current implementation, a ref like `slack-approval-channel` becomes `NOTIFIER_SLACK_SLACK-APPROVAL-CHANNEL_WEBHOOK_URL`, which is not a valid env-var naming convention and will not match the documented contract.

Recommended fix: normalize `config_ref` before env lookup, at minimum converting non-alphanumeric separators like `-` to `_`, and add tests that cover the documented hyphenated examples from the Phase 2 plan.

## Perspective Check
- Functionality: covered; the main round-6 gaps are fixed, but config-ref lookup still does not match the documented contract for real ref names.
- Quality: covered; the current tests pass, but there is still no regression coverage for API-level `notifier_config` / `escalation_config` or for hyphenated config-ref normalization.
- Security: covered; no new trust-boundary regression found in this delta, and Podman advisory host-mount omission is now implemented.

## Verification
- `git status --short`
- `git log -1 --stat --decorate`
- `git diff --name-only fd555e0f61d30191b7c0078c3006624167a7904d..HEAD`
- `git diff fd555e0f61d30191b7c0078c3006624167a7904d..HEAD -- src/agents/workflow/temporal_api.py src/agents/spawner/podman_spawner.py`
- Read: `src/agents/workflow/temporal_api.py`, `src/agents/spawner/podman_spawner.py`, `src/agents/workflow/temporal_activities.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/unit/agents/spawner/test_base.py`, `tests/unit/agents/spawner/test_skills_image.py`, `tests/integration/temporal/test_policy_integration.py`
- `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/integration/temporal/test_policy_integration.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/spawner/test_base.py -q` -> 61 passed

## Summary
This commit fixes the two round-6 blockers structurally: config refs are now reachable from the public workflow start API, and Podman advisory mode now omits host mounts. The phase is still not `LGTM`, though, because the config-ref lookup logic does not yet match the documented Phase 2 contract for hyphenated ref names.
