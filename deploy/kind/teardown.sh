#!/usr/bin/env bash
# Teardown Kind cluster for Phase 1a
set -euo pipefail

export KIND_EXPERIMENTAL_PROVIDER=podman

CLUSTER_NAME="cloud-agents"

echo "Deleting Kind cluster '$CLUSTER_NAME'..."
kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true
echo "Done."
