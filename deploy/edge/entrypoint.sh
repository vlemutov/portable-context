#!/bin/sh
set -eu
umask 077
# Values are supplied through Cloud.ru Secret Management, never image layers.
: "${PC_EDGE_CONFIG_B64:?Missing edge configuration}"
: "${PC_EDGE_CA_B64:?Missing origin trust root}"
: "${PC_EDGE_CERT_B64:?Missing client certificate}"
: "${PC_EDGE_KEY_B64:?Missing client key}"
mkdir -p /tmp/pc-edge
printf '%s' "$PC_EDGE_CONFIG_B64" | base64 -d > /tmp/pc-edge/Caddyfile
printf '%s' "$PC_EDGE_CA_B64" | base64 -d > /tmp/pc-edge/origin-ca.pem
printf '%s' "$PC_EDGE_CERT_B64" | base64 -d > /tmp/pc-edge/edge.pem
printf '%s' "$PC_EDGE_KEY_B64" | base64 -d > /tmp/pc-edge/edge-key.pem
unset PC_EDGE_CONFIG_B64 PC_EDGE_CA_B64 PC_EDGE_CERT_B64 PC_EDGE_KEY_B64
exec caddy run --config /tmp/pc-edge/Caddyfile --adapter caddyfile
