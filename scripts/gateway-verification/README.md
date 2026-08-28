# Gateway verification scripts

Ad hoc scripts used to verify `OpenShellSpawner` behavior against real
OpenShell gateways (local-infra, Kind, real OCP, hosted staging), generalized
for reuse. Background and gateway-specific setup notes (OIDC login, TLS/CA
gotchas, provider setup, known ingress limitations) are in
[`docs/testing-against-openshell-gateways.md`](../../docs/testing-against-openshell-gateways.md).

All scripts read configuration from env vars (see each script's docstring)
and are run with `uv run python scripts/gateway-verification/<script>.py`
from the repo root.

| Script | Verifies |
|---|---|
| `verify_allowed_skills.py` | Per-step `allowed_skills` Landlock scoping (issue #204/#205/#206) — an allowed skill is readable, an unlisted one isn't. |
| `verify_credential_provider_fix.py` | The #199 fix — credentials fail closed without TLS, and the real value never appears in any sandboxed process's env. |
| `verify_provider_cleanup_on_failure.py` | The #214 fix — a post-create spawn failure (forced deterministically via an image with no server on it) does not orphan the credential Provider on the gateway. |
| `diagnose_sandbox.py` | General-purpose diagnostic when a spawn fails post-create — creates a sandbox and stops short of the failure-prone steps so it survives for inspection (normally `spawner.spawn()` auto-deletes on failure). |

Get a bearer token for an OIDC-registered gateway with
[`scripts/openshell-refresh-token.sh`](../openshell-refresh-token.sh).
