# Testing OpenShellSpawner against real gateways

Notes from verifying `allowed_skills` (and general spawn) behavior against
three real OpenShell gateways beyond the local Podman-compose stack
(`~/ws/local-infra`, see its `local-infra` skill): a Kind cluster running the
OpenShell gateway with the kubernetes compute driver, a real OCP cluster with
OpenShell deployed, and a hosted staging gateway with no underlying-cluster
access at all. Each has a different auth/network shape; this doc collects
what's needed for each so the next verification pass doesn't re-derive it.

## 1. Get a CLI that matches the gateway's feature set

The Homebrew/release `openshell` CLI can lag the gateway's actual protocol
support (missing flags like `--oidc-issuer`/`--oidc-client-id`/
`--oidc-audience`). If a flag is missing, build from the vendored source:

```bash
cd ~/ws/OpenShell
cargo build --release -p openshell-cli
# binary at target/release/openshell
export PATH="$HOME/ws/OpenShell/target/release:$PATH"
openshell --version
```

## 2. Register the gateway

For gateways with a plain bearer token or no auth (e.g. local-infra), pass
`--gateway-insecure` or a static token directly to the SDK/spawner. For OIDC
gateways (real OCP, staging), log in interactively — this opens a real
browser even from a background job, because the job runs on your actual
local machine:

```bash
openshell gateway add \
  --name <gateway-nickname> \
  --oidc-issuer <keycloak-realm-url> \
  --oidc-client-id <client-id> \
  --oidc-audience <client-id> \
  <gateway-host>:443
```

Token state lands in `~/.config/openshell/gateways/<name>/oidc_token.json`
(`access_token`, `refresh_token`, `expires_at`, `issuer`, `client_id`). TTLs
observed as short as ~300s. Any CLI call against that gateway (e.g.
`openshell -g <name> sandbox list`) silently refreshes it — run one
immediately before extracting the token for a Python script:

```bash
openshell -g <name> sandbox list >/dev/null 2>&1
python3 -c "
import json
d = json.load(open('$HOME/.config/openshell/gateways/<name>/oidc_token.json'))
print(d['access_token'])
"
```

## 2a. TLS for public-CA (Let's Encrypt) gateways

`build_spawner()`'s `tls_ca` param only enables TLS on the client if it's
truthy (`src/cloud_agents/spawner/factory.py`, `build_spawner()`: `if tls_ca:
... TlsConfig(ca_path=Path(tls_ca))`) — passing nothing means the client
attempts a **plaintext** connection, which fails against an `https://`
gateway. For a gateway with a public CA (e.g. a hosted staging gateway behind
Let's Encrypt), point `GATEWAY_TLS_CA` at the certifi bundle inside the
**project's own venv** (not the system Python's — a system Python may have a
different or missing bundle):

```bash
uv run python -c "import certifi; print(certifi.where())"
```

(`openshell.TlsConfig()` with no args also supports "system roots" per its
docstring, but the verification scripts here only expose a `tls_ca` path
param, so certifi's bundle is the practical equivalent.)

## 3. Wire up a real (non-mocked) LLM provider on the gateway

Gateways can be network-locked so sandboxes can *only* reach the gateway's
own internal inference proxy (`https://inference.local`), not the public
internet — plain `LIGHTSPEED_PROVIDER=openai` pointed at `api.openai.com`
will fail even though the gateway itself has outbound access. Route through
the gateway's own provider mechanism instead:

```bash
openshell -g <name> provider create \
  --name my-openai \
  --type openai \
  --credential OPENAI_API_KEY="$OPENAI_API_KEY"

openshell -g <name> inference set --provider my-openai --model gpt-4o-mini
```

`openshell provider create --type openai --help`-style discovery isn't
needed — the type strings are fixed; see
`crates/openshell-providers/src/lib.rs::normalize_provider_type` in the
OpenShell source for the full list (`openai`, `anthropic`, `nvidia`,
`copilot`, `google-vertex-ai`/`vertex`, `gitlab`, `github`, `claude-code`,
`generic`).

**Known gap (fixed by issue #244 when going through `OpenShellSpawner`,
still a manual step for raw `openshell` CLI use above): OpenShell ships no
builtin `ProviderProfile` for `openai`/`anthropic`.** Only
aws/aws-bedrock/aws-s3/claude-code/codex/copilot/cursor/deepinfra/github/
google-cloud/google-vertex-ai/nvidia/pypi have a builtin profile
(`crates/openshell-providers/src/profiles.rs`) -- confirmed by reading the
gateway source, not by guessing from the vendored proto stubs. A `Provider`
created with `type=openai` (via the CLI above, or a raw `CreateProvider`
gRPC call) still "succeeds" with no error, but the gateway silently skips
both credential-env-var injection and network-egress policy for it,
logging `"provider type has no profile; skipping provider policy layer"` --
the symptom is a sandboxed agent reporting `Missing credentials` even
though `GetInferenceBundle`/routing resolves fine. `OpenShellSpawner`
(Python, this repo) now calls `_ensure_provider_profile()` before
`_create_provider()` to idempotently import a bundled profile
(`spawner/provider_profiles.py`) scoped to its own workspace, so this is
transparent when spawning through `cloud_agents`. If you hit this symptom
driving the gateway directly via the `openshell` CLI (not through
`cloud_agents`), register one yourself first:
`openshell -g <name> provider profile import -f openai.yaml` (no bundled
`openai.yaml`/`anthropic.yaml` ships with OpenShell either -- write one
matching the shape of `providers/nvidia.yaml` in the OpenShell source,
substituting the host and env var).

Then spawn sandboxes with:

```python
env = {
    "LIGHTSPEED_PROVIDER": "openai",
    "LIGHTSPEED_PROVIDER_URL": "https://inference.local",
    "LIGHTSPEED_MODEL": "gpt-4o-mini",
    "OPENAI_API_KEY": "unused",  # gateway injects the real credential; only needs to be non-empty
}
```

`OpenShellSpawner._build_network_policy()` already grants Landlock egress to
`LIGHTSPEED_PROVIDER_URL`'s host automatically — no extra policy wiring
needed on the caller's side.

**A bare `curl https://inference.local/` returning
`{"error":"connection not allowed by policy"}` (HTTP 403) is expected, not a
bug** — the gateway's inference proxy only forwards recognized inference API
paths (e.g. `POST /v1/chat/completions`), and denies anything else
(`openshell-supervisor-network/src/proxy.rs`, "Not an inference request —
deny"). Don't use a bare GET as a smoke test; use the real app instead (see
§5).

## 4. Build and push a sandbox image for the gateway's node architecture

Local Kind on Apple Silicon needs `arm64`; real OCP/staging gateways are
typically `amd64`. Build both when unsure:

```bash
cd ~/ws/lightspeed-agentic-sandbox
podman build --security-opt label=disable --platform linux/amd64 \
  -t quay.io/<you>/lightspeed-agentic-sandbox:latest-amd64 .
podman push quay.io/<you>/lightspeed-agentic-sandbox:latest-amd64
```

Watch for **stale image cache** on Kind nodes (`imagePullPolicy:
IfNotPresent` for non-`latest` tags reuses an old layer even after a push).
Symptom: `materialize-skills.sh: No such file or directory` even though the
script is definitely in the image you just pushed. Fix — evict the cached
image from the node's CRI store (this Kind setup uses the podman provider,
so it's `podman exec`, not `docker exec`):

```bash
podman exec local-infra-control-plane crictl rmi <image>
```

## 5. Manually exec the app to debug without waiting on `spawn()`'s cleanup

`spawner.spawn()` deletes the sandbox automatically on any post-create
failure ("deleting sandbox to prevent orphan"), which destroys the exact
evidence you need to debug. To inspect a live failure, replicate
`_do_spawn()`'s steps by hand against the low-level client instead of
calling `spawn()`:

```python
spec = openshell_pb2.SandboxSpec(template=openshell_pb2.SandboxTemplate(image=IMAGE, labels={}))
for k, v in env.items():
    spec.environment[k] = v
spawner._build_network_policy(spec, env)
spawner._build_baseline_filesystem_policy(spec, allowed_skills=["k8s-diag"])
sandbox_ref = spawner._client.create(workspace="default", spec=spec)
spawner._client.wait_ready(sandbox_ref.name, workspace="default", timeout_seconds=60)
# ... exec whatever you need, without any automatic teardown
```

Two env vars matter when manually exec'ing commands (normally supplied by
`_do_spawn()`'s `self._extra_env` merge, which manual `sandbox exec` calls
don't get automatically):

```
PYTHONPATH=/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages
LIGHTSPEED_SKILLS_DIR=/app/skills
```

Without `PYTHONPATH`, `python3 -c "import lightspeed_agentic.app"` fails
with `ModuleNotFoundError: No module named 'lightspeed_agentic'` even though
the source is present on disk at `/opt/lightspeed/src` — the sandbox image's
Containerfile `ENV PYTHONPATH=...` is **not** inherited by `sandbox exec`;
only `SandboxSpec.environment` and the spawner's own `_extra_env` merge
apply.

To bring up the real app server by hand and hit it from inside the sandbox
(bypassing the gateway's HTTP exposure layer entirely):

```bash
openshell -g <name> sandbox exec -n <sandbox> \
  --env PYTHONPATH=/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages \
  --env LIGHTSPEED_SKILLS_DIR=/app/skills \
  --env LIGHTSPEED_PROVIDER=openai \
  --env LIGHTSPEED_PROVIDER_URL=https://inference.local \
  --env LIGHTSPEED_MODEL=gpt-4o-mini \
  --env OPENAI_API_KEY=unused \
  -- sh -c "nohup python3 -m uvicorn lightspeed_agentic.app:app --host 0.0.0.0 --port 8080 >/tmp/server.log 2>&1 & sleep 5; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/health"
```

## 6. Known gap: HTTP exposure/readiness can 404 on gateways without Host-header multiplexing

`OpenShellSpawner._wait_ready_with_host()` polls the gateway's HTTP-proxied
`/health` route using `ExposeService`'s returned virtual host as a `Host`
header against the *same* `host:port` as the gRPC endpoint. This worked
against local Kind (podman + kubernetes drivers) and a real OCP cluster, but
returned a bare `HTTP/2 404` (no body, no `content-type` — i.e. rejected
before reaching FastAPI) against one hosted staging gateway, even with a
correct `Host` header and a valid bearer token. The app itself was
confirmed healthy the entire time via direct in-sandbox `curl
127.0.0.1:8080/health` (§5) — this is specifically a gap in that gateway's
ingress not supporting Host-header-multiplexed HTTP proxying to arbitrary
sandbox ports on its main TLS port, not a bug in the sandbox image or the
provider wiring. If `spawner.spawn()` times out with "HTTP server did not
become ready" but the manual exec in §5 shows a healthy local `/health`,
this is the same gap — don't waste time re-debugging the app or provider
config.

## 7. Verify `allowed_skills` scoping once the sandbox is up

Whether via full `spawn()` or the manual path in §5, the same three checks
prove per-name skill scoping end-to-end (mirrors
`tests/e2e/test_guardrails.py::test_allowed_skills_scoping_on_real_gateway`):

```python
await spawner._materialize_allowed_skills(sandbox_id, ["k8s-diag"])

r1 = spawner._client.exec(sandbox_id, ["cat", "/skills/k8s-diag/SKILL.md"])
assert r1.exit_code == 0                      # allowed skill readable

r2 = spawner._client.exec(sandbox_id, ["cat", "/skills/git-ops/SKILL.md"])
assert r2.exit_code != 0                      # unlisted skill: EACCES via Landlock

r3 = spawner._client.exec(sandbox_id, ["ls", "/app/skills"])
assert "k8s-diag" in r3.stdout and "git-ops" not in r3.stdout
```

## 7a. Fastest loop: run the verification scripts against local-infra

For iterating on `OpenShellSpawner` changes, `scripts/gateway-verification/`
(see its README) wraps §2-§7 into two reusable scripts:

```bash
cd ~/ws/local-infra && make up-openshell   # plaintext, JWT auth, allow_unauthenticated_users=true, Podman driver

GATEWAY_URL=localhost:8080 SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest-arm64 \
  uv run python scripts/gateway-verification/verify_credential_provider_fix.py

GATEWAY_URL=localhost:8080 SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest-arm64 \
  uv run python scripts/gateway-verification/verify_allowed_skills.py
```

Both should print `ALL CHECKS PASSED`. If something fails post-create, use
`scripts/gateway-verification/diagnose_sandbox.py` — it stops short of the
failure-prone steps so the sandbox survives for inspection (`spawner.spawn()`
auto-deletes on any post-create failure, destroying the evidence).

For Kind / real OCP / a hosted staging gateway instead of local-infra: same
scripts, different `GATEWAY_URL`/`GATEWAY_TLS_CA`/`GATEWAY_BEARER_TOKEN` env
vars per §1-§2a above and `scripts/openshell-refresh-token.sh` for the token.

## 8. Testing spawn:ephemeral against a plaintext local gateway

Since the #199 credential-exposure fix, provider creation fails closed on any
non-TLS gateway (`RuntimeError: Provider creation requires TLS
(OPENSHELL_TLS_CA) — refusing to send credentials over insecure channel`).
`local-infra`'s gateway is plaintext by default, so exercising
`spawn:ephemeral` against it needs an explicit opt-in:

```bash
OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1
```

Only use this for known-plaintext dev gateways (e.g. local-infra) — never for
a gateway that's supposed to be TLS-protected, since it disables the
protection the #199 fix added.

## 9. App-level e2e (beyond the spawner-level verification scripts)

The `scripts/gateway-verification/*.py` scripts above verify `OpenShellSpawner`
in isolation. To verify the full stack (workflow YAML → step executor →
spawner → real gateway → real LLM), run the existing real-OpenAI e2e suites
from `lightspeed-stack` (needs `OPENAI_API_KEY`):

```bash
cd ~/ws/lightspeed-stack

# /v1/agents/run + full workflow execution, spawn:none
OPENSHELL_GATEWAY_URL=localhost:8080 \
  uv run pytest tests/e2e/cloud_agents/test_agents_e2e.py tests/e2e/cloud_agents/test_workflow_execution_e2e.py -v

# spawn:none / local / ephemeral matrix against local-infra
OPENSHELL_GATEWAY_URL=localhost:8080 \
LIGHTSPEED_SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest-arm64 \
OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 \
  uv run pytest tests/e2e/cloud_agents/test_spawn_modes_e2e.py -v
```

## Quick reference: env vars per driver/gateway

| Gateway | Compute driver | TLS CA | Auth |
|---|---|---|---|
| `~/ws/local-infra` | podman | disabled (plaintext) | none |
| Kind (this workspace) | podman or kubernetes | disabled or self-signed | none / static token |
| Real OCP | kubernetes | secret varies **per Route** — a passthrough Route's serving cert may be signed by a *different* CA than other TLS secrets in the namespace; always pull the CA from the exact secret the Route references | OIDC bearer token; `iss` claim must match the token request's `Host` header exactly (e.g. `-H "Host: keycloak.keycloak.svc.cluster.local:9090"` even when reaching Keycloak via `localhost` port-forward) |
| Hosted staging (no cluster access) | kubernetes | public CA (`certifi.where()` — Let's Encrypt) | OIDC bearer token via `openshell gateway add --oidc-*` |
