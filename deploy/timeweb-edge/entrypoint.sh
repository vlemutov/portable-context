#!/bin/sh
set -eu
umask 077
mkdir -p /tmp/pc-edge
# The first deployment obtains the technical domain before certificates exist.
# Partial configuration must fail closed, never silently replace a live gateway.
if [ -z "${PC_EDGE_CONFIG_B64:-}${PC_EDGE_CA_B64:-}${PC_EDGE_CERT_B64:-}${PC_EDGE_KEY_B64:-}" ]; then
  echo 'pc-edge: preparing setup listener on 8080' >&2
  printf '%s\n' '{' ' admin off' ' auto_https off' ' persist_config off' '}' ':8080 {' ' @health path /_pc/health' ' respond @health "Portable Context setup" 200' ' respond "Setup in progress" 503' '}' > /tmp/pc-edge/Caddyfile
else
  echo 'pc-edge: preparing configured listener on 8080' >&2
  : "${PC_EDGE_CONFIG_B64:?Missing config}" "${PC_EDGE_CA_B64:?Missing CA}" "${PC_EDGE_CERT_B64:?Missing certificate}" "${PC_EDGE_KEY_B64:?Missing key}"
  printf '%s' "$PC_EDGE_CONFIG_B64" | base64 -d > /tmp/pc-edge/Caddyfile
  printf '%s' "$PC_EDGE_CA_B64" | base64 -d > /tmp/pc-edge/origin-ca.pem
  printf '%s' "$PC_EDGE_CERT_B64" | base64 -d > /tmp/pc-edge/edge.pem
  printf '%s' "$PC_EDGE_KEY_B64" | base64 -d > /tmp/pc-edge/edge-key.pem
fi
unset PC_EDGE_CONFIG_B64 PC_EDGE_CA_B64 PC_EDGE_CERT_B64 PC_EDGE_KEY_B64
echo 'pc-edge: validating configuration' >&2
caddy validate --config /tmp/pc-edge/Caddyfile --adapter caddyfile >/dev/null
echo 'pc-edge: starting listener on 8080' >&2
exec caddy run --config /tmp/pc-edge/Caddyfile --adapter caddyfile
