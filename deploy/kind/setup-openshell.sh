#!/usr/bin/env bash
# Setup Kind cluster with cloud agents using OpenShell spawner
#
# Deploys:
#   - Kind cluster with Podman socket mount
#   - PostgreSQL + Temporal
#   - OpenShell gateway (DooD — Podman driver via host socket)
#   - Workflow runner (OpenShell spawner)
#   - MCP servers (filesystem, kubectl)
#
# Prerequisites:
#   - kind, podman, kubectl
#   - Images built: workflow-runner, lightspeed-agentic-sandbox,
#     openshell-gateway, mcp-filesystem, mcp-kubectl
#
# Environment:
#   OPENAI_API_KEY — required

set -euo pipefail

CLUSTER_NAME="cloud-agents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export KIND_EXPERIMENTAL_PROVIDER=podman

echo "=== Cloud Agents Kind Setup (OpenShell) ==="

# Check prerequisites
for cmd in kind podman kubectl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd is required but not found"
        exit 1
    fi
done

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "ERROR: OPENAI_API_KEY is required"
    exit 1
fi

# Delete existing cluster
kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true

# Determine Podman socket path (Linux rootless)
PODMAN_SOCK="/run/user/$(id -u)/podman/podman.sock"
if [ ! -S "$PODMAN_SOCK" ]; then
    PODMAN_SOCK="/run/podman/podman.sock"
fi
echo "[info] Podman socket: $PODMAN_SOCK"

# Create Kind config with socket mount
cat > /tmp/kind-config-openshell.yaml <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${CLUSTER_NAME}
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: ${PODMAN_SOCK}
        containerPath: /run/podman/podman.sock
YAML

echo "[kind] Creating cluster..."
kind create cluster --config /tmp/kind-config-openshell.yaml --wait 60s

# Load images into Kind
IMAGES=(
    "localhost/workflow-runner:latest"
    "localhost/lightspeed-agentic-sandbox:latest"
    "localhost/openshell-gateway:latest"
    "localhost/mcp-filesystem:latest"
    "localhost/mcp-kubectl:latest"
)

for img in "${IMAGES[@]}"; do
    echo "[kind] Loading $img..."
    podman save "$img" -o /tmp/img.tar
    kind load image-archive /tmp/img.tar --name "$CLUSTER_NAME"
    rm -f /tmp/img.tar
done

# Tag images inside Kind node
echo "[kind] Tagging images..."
podman exec "${CLUSTER_NAME}-control-plane" ctr --namespace k8s.io images tag \
    localhost/lightspeed-agentic-sandbox:latest docker.io/library/lightspeed-agentic-sandbox:latest
podman exec "${CLUSTER_NAME}-control-plane" ctr --namespace k8s.io images tag \
    localhost/mcp-filesystem:latest docker.io/library/mcp-filesystem:latest
podman exec "${CLUSTER_NAME}-control-plane" ctr --namespace k8s.io images tag \
    localhost/mcp-kubectl:latest docker.io/library/mcp-kubectl:latest

# Also need supervisor image — pull and load it
echo "[kind] Loading OpenShell supervisor image..."
podman pull ghcr.io/nvidia/openshell/supervisor:latest 2>/dev/null || true
podman save ghcr.io/nvidia/openshell/supervisor:latest -o /tmp/img.tar
kind load image-archive /tmp/img.tar --name "$CLUSTER_NAME"
rm -f /tmp/img.tar

# Deploy infrastructure
echo "[kind] Deploying PostgreSQL..."
kubectl apply -f "$SCRIPT_DIR/postgres.yaml"
kubectl wait --for=condition=ready pod -l app=postgres --timeout=60s

echo "[kind] Deploying Temporal..."
kubectl apply -f "$SCRIPT_DIR/temporal.yaml"
kubectl wait --for=condition=ready pod -l app=temporal-server --timeout=120s

# Create secrets
kubectl create secret generic llm-api-key \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" 2>/dev/null || true
kubectl create secret generic openai-api-key \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" 2>/dev/null || true
kubectl create secret generic anthropic-api-key \
    --from-literal=ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" 2>/dev/null || true

# Generate JWT keys for OpenShell gateway
echo "[jwt] Generating Ed25519 signing keys..."
openssl genpkey -algorithm Ed25519 -out /tmp/jwt-private.pem 2>/dev/null
openssl pkey -in /tmp/jwt-private.pem -pubout -out /tmp/jwt-public.pem 2>/dev/null
echo "openshell-gateway-key-1" > /tmp/jwt-kid.txt

kubectl create secret generic openshell-jwt-keys \
    --from-file=private.pem=/tmp/jwt-private.pem \
    --from-file=public.pem=/tmp/jwt-public.pem \
    --from-file=kid.txt=/tmp/jwt-kid.txt \
    2>/dev/null || true
rm -f /tmp/jwt-private.pem /tmp/jwt-public.pem /tmp/jwt-kid.txt

# Create demo data ConfigMap
kubectl create configmap demo-data \
    --from-file="$REPO_ROOT/examples/demo-data/" 2>/dev/null || true

# Apply RBAC and NetworkPolicy
echo "[kind] Applying RBAC and NetworkPolicy..."
kubectl apply -f "$SCRIPT_DIR/rbac.yaml"
kubectl apply -f "$SCRIPT_DIR/network-policy.yaml"

# Deploy OpenShell gateway
echo "[kind] Deploying OpenShell gateway..."
kubectl apply -f "$SCRIPT_DIR/openshell-gateway.yaml"
kubectl wait --for=condition=ready pod -l app=openshell-gateway --timeout=120s

# Deploy workflow runner (OpenShell variant)
echo "[kind] Deploying workflow runner (OpenShell spawner)..."
kubectl apply -f "$SCRIPT_DIR/workflow-runner-openshell.yaml"
kubectl wait --for=condition=ready pod -l app=workflow-runner --timeout=60s

# Deploy MCP servers
kubectl apply -f "$REPO_ROOT/examples/kind-mcp-filesystem.yaml"
kubectl wait --for=condition=ready pod -l app=mcp-filesystem --timeout=60s
kubectl apply -f "$REPO_ROOT/examples/kind-mcp-kubectl.yaml"
kubectl wait --for=condition=ready pod -l app=mcp-kubectl --timeout=60s

echo ""
echo "=== Setup Complete ==="
echo ""
kubectl get pods
echo ""
echo "To test:"
echo "  kubectl port-forward svc/workflow-runner 8080:8080 &"
echo "  curl http://localhost:8080/readyz"
echo ""
echo "To teardown:"
echo "  KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name $CLUSTER_NAME"
