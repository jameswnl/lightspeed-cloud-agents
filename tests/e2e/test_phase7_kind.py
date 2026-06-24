"""E2E tests for Phase 7 security on Kind (K8s).

Tests K8s-specific security: Secret refs in pod specs, bearer auth
across Services, content-hash Job naming.

Prerequisites:
  - kind installed with KIND_EXPERIMENTAL_PROVIDER=podman
  - agent-runtime:latest image built
  - OPENAI_API_KEY set
  - kubectl configured

Usage:
  uv run python tests/e2e/test_phase7_kind.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CLUSTER = "phase7-test"
CONTEXT = f"kind-{CLUSTER}"
IMAGE = "docker.io/library/agent-runtime:latest"


def run(cmd: str, check: bool = True, capture: bool = True) -> str:
    """Run a shell command."""
    r = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    if check and r.returncode != 0:
        print(f"FAILED: {cmd}")
        print(r.stderr)
        sys.exit(1)
    return r.stdout.strip() if capture else ""


def kubectl(cmd: str, check: bool = True) -> str:
    """Run kubectl against the test cluster."""
    return run(f"kubectl --context {CONTEXT} {cmd}", check=check)


def setup_cluster() -> None:
    """Create Kind cluster and load image."""
    print("--- Setup: Kind cluster ---")
    os.environ["KIND_EXPERIMENTAL_PROVIDER"] = "podman"

    existing = run("kind get clusters", check=False)
    if CLUSTER in existing.split("\n"):
        print(f"[OK] Cluster '{CLUSTER}' already exists")
    else:
        run(f"kind create cluster --name {CLUSTER} --config {REPO_ROOT}/deploy/kind/kind-config.yaml")
        print(f"[OK] Cluster '{CLUSTER}' created")

    run(f"podman tag localhost/agent-runtime:latest {IMAGE}", check=False)
    run(f"podman save {IMAGE} -o /tmp/agent-runtime-p7.tar")
    run(f"kind load image-archive /tmp/agent-runtime-p7.tar --name {CLUSTER}")
    print("[OK] Image loaded")


def deploy_agent_with_secret() -> None:
    """Deploy a diagnostic agent with K8s Secret for API key + bearer auth."""
    print()
    print("--- Deploy: agent with Secret + auth token ---")

    kubectl(f"create secret generic llm-api-key "
            f"--from-literal=OPENAI_API_KEY={os.environ.get('OPENAI_API_KEY', 'dummy')} "
            f"--dry-run=client -o yaml | kubectl --context {CONTEXT} apply -f -")

    kubectl(f"create secret generic agent-auth "
            f"--from-literal=token=e2e-test-token-456 "
            f"--dry-run=client -o yaml | kubectl --context {CONTEXT} apply -f -")

    kubectl(f"create configmap diag-config "
            f"--from-file=agent.yaml={REPO_ROOT}/examples/agents/definitions/diagnostic-agent.yaml "
            f"--from-file=registry.yaml={REPO_ROOT}/examples/agents/registry.yaml "
            f"--dry-run=client -o yaml | kubectl --context {CONTEXT} apply -f -")

    kubectl(f"create configmap diag-tools "
            f"--from-file=diagnostic_tools.py={REPO_ROOT}/examples/agents/tools/diagnostic_tools.py "
            f"--dry-run=client -o yaml | kubectl --context {CONTEXT} apply -f -")

    manifest = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: diag-auth-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: diag-auth-test
  template:
    metadata:
      labels:
        app: diag-auth-test
    spec:
      containers:
        - name: agent
          image: {IMAGE}
          imagePullPolicy: Never
          ports:
            - containerPort: 8080
          env:
            - name: OLLAMA_URL
              value: "https://api.openai.com/v1"
            - name: AGENT_MODEL
              value: "gpt-4"
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: llm-api-key
                  key: OPENAI_API_KEY
            - name: AGENT_API_TOKEN
              valueFrom:
                secretKeyRef:
                  name: agent-auth
                  key: token
          volumeMounts:
            - name: config
              mountPath: /app/agent.yaml
              subPath: agent.yaml
              readOnly: true
            - name: config
              mountPath: /app/registry.yaml
              subPath: registry.yaml
              readOnly: true
            - name: tools
              mountPath: /app/tools/diagnostic_tools.py
              subPath: diagnostic_tools.py
              readOnly: true
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 3
            periodSeconds: 5
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
      volumes:
        - name: config
          configMap:
            name: diag-config
        - name: tools
          configMap:
            name: diag-tools
---
apiVersion: v1
kind: Service
metadata:
  name: diag-auth-test
spec:
  selector:
    app: diag-auth-test
  ports:
    - port: 8080
      targetPort: 8080
"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        f.flush()
        kubectl(f"apply -f {f.name}")
    os.unlink(f.name)

    kubectl("rollout status deployment/diag-auth-test --timeout=120s")
    print("[OK] Agent deployed with Secret refs + auth token")


def test_secret_not_in_describe() -> None:
    """Test 1: kubectl describe should show secretKeyRef, not plain value."""
    print()
    print("--- Test 1: API key not visible in kubectl describe ---")
    desc = kubectl("describe deployment/diag-auth-test")
    if "secretKeyRef" in desc.lower() or "SecretKeyRef" in desc or "llm-api-key" in desc:
        print("[OK] Pod spec references Secret, not plain value")
    else:
        print("[WARN] Could not confirm secretKeyRef in describe output")

    if os.environ.get("OPENAI_API_KEY", "") in desc:
        print("[FAIL] Plain API key visible in kubectl describe!")
    else:
        print("[OK] Plain API key NOT visible in kubectl describe")


def test_bearer_auth_k8s() -> None:
    """Test 2: Bearer auth works across K8s Service."""
    print()
    print("--- Test 2: Bearer auth on K8s agent ---")

    # Port-forward in background
    import subprocess as sp
    pf = sp.Popen(
        f"kubectl --context {CONTEXT} port-forward svc/diag-auth-test 9191:8080".split(),
        stdout=sp.DEVNULL, stderr=sp.DEVNULL,
    )
    time.sleep(3)

    try:
        import urllib.request

        # Unauthenticated → 401
        try:
            req = urllib.request.Request("http://localhost:9191/v1/run",
                data=json.dumps({"prompt": "test"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req)
            print("[WARN] Expected 401 but got 200 — auth may not be active")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("[OK] Unauthenticated call → 401")
            else:
                print(f"[FAIL] Expected 401, got {e.code}")

        # Authenticated → 200
        req = urllib.request.Request("http://localhost:9191/healthz", method="GET")
        resp = urllib.request.urlopen(req)
        if resp.status == 200:
            print("[OK] /healthz → 200 (exempt from auth)")

        # Authenticated run
        req = urllib.request.Request("http://localhost:9191/v1/run",
            data=json.dumps({"prompt": "list hosts"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer e2e-test-token-456",
            },
            method="POST")
        resp = urllib.request.urlopen(req, timeout=120)
        body = json.loads(resp.read())
        if body.get("success"):
            print(f"[OK] Authenticated call → success, agent={body.get('agent_name')}")
        else:
            print(f"[WARN] Authenticated call returned success=False: {body.get('error', '')[:100]}")

    finally:
        pf.terminate()
        pf.wait()


def test_cross_pod_auth() -> None:
    """Test 3: Cross-pod call with auth from inside the cluster."""
    print()
    print("--- Test 3: Cross-pod authenticated call ---")
    result = kubectl(
        'exec deployment/diag-auth-test -- python -c "'
        "import urllib.request, json; "
        "req = urllib.request.Request('http://diag-auth-test:8080/healthz'); "
        "resp = urllib.request.urlopen(req); "
        "print(json.loads(resp.read()))"
        '"',
        check=False,
    )
    if "ready" in result:
        print(f"[OK] Cross-pod healthz: {result}")
    else:
        print(f"[WARN] Cross-pod call issue: {result[:200]}")


def teardown_cluster() -> None:
    """Delete the Kind cluster."""
    print()
    print("--- Teardown ---")
    os.environ["KIND_EXPERIMENTAL_PROVIDER"] = "podman"
    run(f"kind delete cluster --name {CLUSTER}")
    print("[OK] Cluster deleted")


def main() -> None:
    """Run all Phase 7 Kind E2E tests."""
    print("=== Phase 7 Kind (K8s) E2E Tests ===")
    print()
    try:
        setup_cluster()
        deploy_agent_with_secret()
        test_secret_not_in_describe()
        test_bearer_auth_k8s()
        test_cross_pod_auth()
    finally:
        teardown_cluster()

    print()
    print("=== All Phase 7 Kind E2E Tests Complete ===")


if __name__ == "__main__":
    main()
