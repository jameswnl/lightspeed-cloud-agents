#!/usr/bin/env bash
# Refresh and print the current OIDC access token for a registered OpenShell
# gateway. OIDC token TTLs observed as short as ~300s -- run this
# immediately before a Python verification script needs the token, not
# minutes ahead of time. See docs/testing-against-openshell-gateways.md.
#
# Usage: scripts/openshell-refresh-token.sh <gateway-nickname>
set -euo pipefail

GATEWAY="${1:?usage: $0 <gateway-nickname>}"
TOKEN_FILE="$HOME/.config/openshell/gateways/$GATEWAY/oidc_token.json"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "No OIDC token file for gateway '$GATEWAY' at $TOKEN_FILE" >&2
  echo "Register it first: openshell gateway add --name $GATEWAY --oidc-issuer ... <host>:443" >&2
  exit 1
fi

# Any CLI call against the gateway silently refreshes the token file.
openshell -g "$GATEWAY" sandbox list >/dev/null 2>&1 || true

python3 -c "
import json
with open('$TOKEN_FILE') as f:
    print(json.load(f)['access_token'])
"
