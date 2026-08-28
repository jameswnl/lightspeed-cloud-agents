#!/usr/bin/env python3
"""Verify the #224 fix (orphan reconciliation across a process restart) against
a real gateway.

Issue #224: `reconcile_orphaned_sandboxes()` (workflow-runner startup) is
supposed to find and destroy sandboxes left running by a *previous* process
instance, via `spawner.list_active({"spawned-by": "workflow-runner"})` then
`spawner.destroy(name)` for each result. Before the fix, `_do_list_active()`
read only the current process's in-memory `_sandbox_names` dict -- always
empty after a restart, so orphans were never found. The fix durably labels
sandboxes at create time (`spawned-by=workflow-runner`,
`cloud-agents/agent-name=<agent_name>`) and has `_do_list_active()`/
`_do_destroy()` query the gateway's `ListSandboxes` RPC directly instead.

This script creates a sandbox directly via the SDK (bypassing the full
`spawner.spawn()` orchestration, which requires a real lightspeed_agentic
server image and LLM provider to reach HTTP-readiness -- irrelevant to what
#224 touches) with exactly the labels `_do_spawn()` would attach. It then
builds a *second*, independent `OpenShellSpawner` instance (simulating a
restarted process, with an empty `_sandbox_names`) and exercises the real
reconciliation loop: list_active(filter) -> assert found -> destroy(name) ->
assert actually gone from the gateway.

Checks:
  1. The sandbox is created on the gateway with the expected labels.
  2. A fresh spawner instance's list_active({"spawned-by": "workflow-runner"})
     finds the agent_name via the cloud-agents/agent-name label -- proves
     discovery does not depend on any in-memory state from the spawn.
  3. The fresh spawner's destroy(agent_name) succeeds.
  4. The sandbox is actually gone from the gateway afterward (GetSandbox ->
     NOT_FOUND) -- proves real deletion, not a silent no-op.

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway (e.g. localhost:9090
                    for the Kind kubernetes-driver gateway via
                    `make kind-openshell-port-forward` in ~/ws/local-infra)

Optional env vars:
  GATEWAY_WORKSPACE       default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit for plaintext gateways
  GATEWAY_BEARER_TOKEN     OIDC/bearer token; see scripts/openshell-refresh-token.sh
  VERIFY_IMAGE             image for the throwaway sandbox; default: "busybox:latest"

Usage (local Kind gateway, no auth):
  GATEWAY_URL=localhost:9090 \\
    uv run python scripts/gateway-verification/verify_orphan_reconciliation.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import grpc

from cloud_agents.spawner.factory import build_spawner

GATEWAY_URL = os.environ["GATEWAY_URL"]
WORKSPACE = os.environ.get("GATEWAY_WORKSPACE", "default")
TLS_CA = os.environ.get("GATEWAY_TLS_CA") or None
BEARER_TOKEN = os.environ.get("GATEWAY_BEARER_TOKEN") or None
VERIFY_IMAGE = os.environ.get("VERIFY_IMAGE", "busybox:latest")

# Unique per run so repeated executions (or a failed prior run's leftovers)
# never collide on the cloud-agents/agent-name label value.
AGENT_NAME = f"verify-224-orphan-{uuid.uuid4().hex[:12]}"
SPAWNED_BY_LABEL = {"spawned-by": "workflow-runner"}
AGENT_NAME_LABEL_KEY = "cloud-agents/agent-name"


def _new_spawner():
    return build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )


async def main() -> None:
    if not TLS_CA:
        os.environ.setdefault("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", "1")

    from openshell._proto import openshell_pb2

    spawner = _new_spawner()

    # Create directly via the SDK with exactly the labels _do_spawn() attaches
    # -- skips the full spawn() orchestration (HTTP readiness, start_server),
    # which needs a real lightspeed_agentic image/provider and is orthogonal
    # to what #224 touches (label attachment + gateway-side discovery).
    spec = openshell_pb2.SandboxSpec(template=openshell_pb2.SandboxTemplate(image=VERIFY_IMAGE))
    sandbox_labels = {**SPAWNED_BY_LABEL, AGENT_NAME_LABEL_KEY: AGENT_NAME}
    sandbox_ref = await asyncio.to_thread(
        spawner._client.create,
        workspace=WORKSPACE,
        spec=spec,
        labels=sandbox_labels,
    )
    print(f"PASS (setup): created sandbox '{sandbox_ref.name}' with labels {sandbox_labels}")

    try:
        # Simulate a restarted process: a brand-new spawner instance, with an
        # empty _sandbox_names, that never saw the create() call above.
        fresh_spawner = _new_spawner()
        assert fresh_spawner._sandbox_names == {}

        found = await fresh_spawner.list_active(SPAWNED_BY_LABEL)
        if AGENT_NAME not in found:
            print(
                f"FAIL (1): list_active({SPAWNED_BY_LABEL}) on a fresh spawner instance "
                f"did not find '{AGENT_NAME}'. Found: {found}"
            )
            sys.exit(1)
        print(f"PASS (1): fresh spawner's list_active() found '{AGENT_NAME}' via the gateway")

        await fresh_spawner.destroy(AGENT_NAME)
        print(f"PASS (2): fresh spawner's destroy('{AGENT_NAME}') completed without raising")

        from openshell._proto import openshell_pb2_grpc

        def _sync_get_sandbox():
            channel = spawner._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)
                return stub.GetSandbox(
                    openshell_pb2.GetSandboxRequest(name=sandbox_ref.name, workspace=WORKSPACE)
                )
            finally:
                channel.close()

        try:
            resp = await asyncio.to_thread(_sync_get_sandbox)
            print(
                f"FAIL (3): sandbox '{sandbox_ref.name}' still exists on the gateway after "
                f"destroy() -- ORPHAN NOT CLEANED UP:\n   ",
                resp,
            )
            sys.exit(1)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                print(f"PASS (3): sandbox '{sandbox_ref.name}' no longer exists on the gateway")
            else:
                print(f"FAIL (3): unexpected error looking up sandbox '{sandbox_ref.name}':", exc)
                sys.exit(1)

        print(
            "\nALL CHECKS PASSED -- #224 fix confirmed: a fresh process instance "
            "discovers and destroys an orphaned sandbox via the gateway's durable "
            "labels, with no reliance on in-memory state from the original spawn."
        )
    except BaseException:
        # Best-effort cleanup if any assertion above failed before destroy() ran.
        try:
            await asyncio.to_thread(spawner._client.delete, sandbox_ref.name, workspace=WORKSPACE)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main())
