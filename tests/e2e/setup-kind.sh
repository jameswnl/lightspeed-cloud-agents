#!/usr/bin/env bash
# Setup Kind cluster with Temporal Server for E2E tests.
#
# Usage:
#   ./tests/e2e/temporal/setup-kind.sh        # create cluster + deploy Temporal
#   ./tests/e2e/temporal/setup-kind.sh --run   # also run E2E tests
#
# Prerequisites:
#   - kind
#   - podman (machine must be running)
#   - kubectl
#
# To tear down:
#   ./tests/e2e/temporal/teardown-kind.sh

set -euo pipefail

CLUSTER_NAME="temporal-e2e"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy/kind"

export KIND_EXPERIMENTAL_PROVIDER=podman

echo "=== Temporal E2E Kind Setup ==="

# Check prerequisites
for cmd in kind podman kubectl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd is required but not found"
        exit 1
    fi
done

# Ensure Podman machine is running
if ! podman info &>/dev/null; then
    echo "Starting Podman machine..."
    podman machine start
fi

# Create or reuse cluster
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "Cluster '$CLUSTER_NAME' already exists, reusing..."
    # Restart container if stopped
    if ! kubectl --context "kind-${CLUSTER_NAME}" cluster-info &>/dev/null; then
        echo "Restarting cluster container..."
        podman start "${CLUSTER_NAME}-control-plane"
        sleep 15
    fi
else
    echo "Creating Kind cluster '$CLUSTER_NAME'..."
    kind create cluster --name "$CLUSTER_NAME" --wait 60s
fi

kubectl config use-context "kind-${CLUSTER_NAME}"

# Deploy PostgreSQL
echo "Deploying PostgreSQL..."
kubectl apply -f "$DEPLOY_DIR/postgres.yaml"
kubectl wait --for=condition=available deployment/postgres --timeout=60s

# Create Temporal database
echo "Creating Temporal database..."
POSTGRES_POD=$(kubectl get pods -l app=postgres -o jsonpath='{.items[0].metadata.name}')
POSTGRES_USER=$(kubectl get secret postgres-secret -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
kubectl exec "$POSTGRES_POD" -- psql -U "$POSTGRES_USER" -c "CREATE DATABASE temporal;" 2>/dev/null || true
kubectl exec "$POSTGRES_POD" -- psql -U "$POSTGRES_USER" -c "CREATE DATABASE temporal_visibility;" 2>/dev/null || true

# Deploy Temporal Server
echo "Deploying Temporal Server..."
kubectl apply -f "$DEPLOY_DIR/temporal.yaml"
echo "Waiting for Temporal Server (this may take 30-60s)..."
kubectl wait --for=condition=available deployment/temporal-server --timeout=180s

echo ""
echo "=== Temporal E2E cluster ready ==="
echo ""
echo "To run E2E tests:"
echo "  kubectl port-forward svc/temporal 7233:7233 &"
echo "  uv run pytest tests/e2e/temporal/ -v"
echo ""
echo "To tear down:"
echo "  ./tests/e2e/temporal/teardown-kind.sh"

# Optionally run tests
if [[ "${1:-}" == "--run" ]]; then
    echo "Starting port-forward and running tests..."
    kubectl port-forward svc/temporal 7233:7233 &
    PF_PID=$!
    sleep 5

    cd "$REPO_ROOT"
    uv run pytest tests/e2e/temporal/ -v
    TEST_EXIT=$?

    kill $PF_PID 2>/dev/null
    exit $TEST_EXIT
fi
