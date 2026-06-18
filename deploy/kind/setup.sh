#!/usr/bin/env bash
# Setup Kind cluster with diagnostic agent for Phase 1a
#
# Usage: ./deploy/kind/setup.sh
#
# Prerequisites:
#   - kind (with KIND_EXPERIMENTAL_PROVIDER=podman)
#   - podman
#   - kubectl

set -euo pipefail

CLUSTER_NAME="cloud-agents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export KIND_EXPERIMENTAL_PROVIDER=podman

echo "=== Phase 1a: Cloud Agents Kind Setup ==="
echo ""

# Check prerequisites
for cmd in kind podman kubectl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd is required but not found"
        exit 1
    fi
done

# Create cluster if not exists
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "[kind] Cluster '$CLUSTER_NAME' already exists"
else
    echo "[kind] Creating cluster '$CLUSTER_NAME'..."
    kind create cluster --config "$SCRIPT_DIR/kind-config.yaml"
fi

# Build diagnostic agent image
echo "[build] Building diagnostic-agent image..."
if ! podman build -f "$REPO_ROOT/deploy/diagnostic-agent/Containerfile" \
    -t diagnostic-agent:latest "$REPO_ROOT"; then
    echo "ERROR: Diagnostic agent image build failed"
    exit 1
fi

# Build monitoring agent image
echo "[build] Building monitoring-agent image..."
if ! podman build -f "$REPO_ROOT/deploy/monitoring-agent/Containerfile" \
    -t monitoring-agent:latest "$REPO_ROOT"; then
    echo "ERROR: Monitoring agent image build failed"
    exit 1
fi

# Load images into Kind
echo "[kind] Loading images into cluster..."
kind load docker-image diagnostic-agent:latest --name "$CLUSTER_NAME"
kind load docker-image monitoring-agent:latest --name "$CLUSTER_NAME"

# Deploy Ollama LLM backend
echo "[kind] Deploying Ollama LLM backend..."
kubectl apply -f "$SCRIPT_DIR/ollama.yaml"

# Apply agent manifests
echo "[kind] Applying agent manifests..."
kubectl apply -f "$SCRIPT_DIR/diagnostic-agent.yaml"
kubectl apply -f "$SCRIPT_DIR/monitoring-agent.yaml"

# Wait for readiness
echo "[kind] Waiting for agents to be ready..."
kubectl rollout status deployment/diagnostic-agent --timeout=120s
kubectl rollout status deployment/monitoring-agent --timeout=120s

echo ""
echo "=== Setup Complete ==="
echo ""
kubectl get pods -l app=diagnostic-agent
echo ""
echo "To test:"
echo "  kubectl port-forward svc/diagnostic-agent 8081:8080 &"
echo "  curl http://localhost:8081/healthz"
echo "  curl -X POST http://localhost:8081/v1/run -H 'Content-Type: application/json' -d '{\"prompt\":\"Check hosts\"}'"
echo ""
echo "To teardown:"
echo "  ./deploy/kind/teardown.sh"
