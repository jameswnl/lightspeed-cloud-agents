#!/usr/bin/env bash
# Setup Kind cluster with cloud agents using the generic agent-runtime image
#
# Usage: ./deploy/kind/setup.sh
#
# Prerequisites:
#   - kind (with KIND_EXPERIMENTAL_PROVIDER=podman)
#   - podman
#   - kubectl
#
# Environment:
#   OPENAI_API_KEY  — required for OpenAI-backed agents
#   AGENT_MODEL     — model name (default: gpt-4)
#   OLLAMA_URL      — LLM backend URL (default: https://api.openai.com/v1)

set -euo pipefail

CLUSTER_NAME="cloud-agents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export KIND_EXPERIMENTAL_PROVIDER=podman

echo "=== Cloud Agents Kind Setup ==="
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

# Build the generic agent-runtime image
echo "[build] Building agent-runtime image..."
if ! podman build -f "$REPO_ROOT/deploy/agent-runtime/Containerfile" \
    -t agent-runtime:latest "$REPO_ROOT"; then
    echo "ERROR: agent-runtime image build failed"
    exit 1
fi

# Load image into Kind
echo "[kind] Loading agent-runtime image into cluster..."
kind load docker-image localhost/agent-runtime:latest --name "$CLUSTER_NAME"

# Create ConfigMaps from example agent definitions and tools
echo "[kind] Creating ConfigMaps for agent configs..."
kubectl create configmap diagnostic-agent-config \
    --from-file=agent.yaml="$REPO_ROOT/examples/agents/definitions/diagnostic-agent.yaml" \
    --from-file=registry.yaml="$REPO_ROOT/examples/agents/registry.yaml" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap diagnostic-agent-tools \
    --from-file=diagnostic_tools.py="$REPO_ROOT/examples/agents/tools/diagnostic_tools.py" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap monitoring-agent-config \
    --from-file=agent.yaml="$REPO_ROOT/examples/agents/definitions/monitoring-agent.yaml" \
    --from-file=registry.yaml="$REPO_ROOT/examples/agents/registry.yaml" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap monitoring-agent-tools \
    --from-file=monitoring_tools.py="$REPO_ROOT/examples/agents/tools/monitoring_tools.py" \
    --dry-run=client -o yaml | kubectl apply -f -

# Create secret for API key
if [ -n "${OPENAI_API_KEY:-}" ]; then
    kubectl create secret generic llm-api-key \
        --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
        --dry-run=client -o yaml | kubectl apply -f -
    echo "[kind] LLM API key secret created"
else
    echo "[WARN] OPENAI_API_KEY not set — agents will fail to call LLM"
fi

# Apply RBAC and NetworkPolicy
echo "[kind] Applying RBAC and NetworkPolicy..."
kubectl apply -f "$SCRIPT_DIR/rbac.yaml"
kubectl apply -f "$SCRIPT_DIR/network-policy.yaml"

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
kubectl get pods
echo ""
echo "To test:"
echo "  kubectl port-forward svc/diagnostic-agent 8081:8080 &"
echo "  kubectl port-forward svc/monitoring-agent 8082:8080 &"
echo "  curl http://localhost:8081/healthz"
echo "  curl -X POST http://localhost:8081/v1/run -H 'Content-Type: application/json' -d '{\"prompt\":\"Check hosts\"}'"
echo ""
echo "To teardown:"
echo "  ./deploy/kind/teardown.sh"
