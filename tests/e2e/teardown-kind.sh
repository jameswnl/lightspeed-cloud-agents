#!/usr/bin/env bash
# Tear down the Temporal E2E Kind cluster.
#
# Usage: ./tests/e2e/temporal/teardown-kind.sh

set -euo pipefail

CLUSTER_NAME="temporal-e2e"
export KIND_EXPERIMENTAL_PROVIDER=podman

echo "Deleting Kind cluster '$CLUSTER_NAME'..."
kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true
echo "Done."
