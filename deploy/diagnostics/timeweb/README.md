# Disposable Timeweb transport diagnostic

This is a **test fixture, not a Portable Context server or supported installer**.
It contains no real memory. Deploy only temporarily with a random runtime
`DIAG_KEY` (at least 32 characters), an explicit budget and a cleanup receipt.

Build this directory for `linux/amd64`. It uses a normal Debian image with shell,
port inspection tools, an explicit port and health check. Pocket ID 2.19.2 is
downloaded with the same SHA-256 verification as the production installer.

After HTTPS is ready, the test controller configures the exact origin through
the key-protected `/_diag/configure` endpoint. A fresh synthetic owner and public
PKCE client are created locally. Provider administration is never proxied.
OAuth routes exercise actual Pocket ID, including consent, refresh and revoke.
Synthetic file and SSE routes require a signed, scoped, owner-bound access token
and successful online userinfo verification. Request logging is disabled.

Files (8 MiB each, 16 MiB total) and identity state are ephemeral and disappear
with the container. This intentionally does **not** test durable S3, backup,
browser passkeys or a real agent's MCP client. Never import real data here.

For API update testing, deploy this Dockerfile from a public Git connection at
one exact commit, then call `POST /api/v1/apps/{id}/deploy` with another exact
`commit_sha`; verify `GET /health` reports the new VERSION on the same HTTPS
hostname. This is the Git/Dockerfile deployment route, not registry-tag update.
Delete the app and any Git connection created solely for the test, and verify
the inventory against the pre-test baseline. Stopping an app is not cleanup.

## Verified experiment: 2026-11-16

The same Timeweb app was deployed from commit
`bc0f7ef3f5f75f3127ef019138441965d34d9c1d` (VERSION 1), then updated through
`POST /api/v1/apps/{id}/deploy` to
`4db28520c8c822ad4b82401cbb5e98376488e01c` (VERSION 2).
Both deployments completed successfully; the API update took 59 seconds between
the platform's recorded start and completion. The app ID and HTTPS origin stayed
the same. This verifies the Git/Dockerfile route, not registry-tag updates.

Both versions passed the public HTTPS probes:

| Check | Observed result |
| --- | --- |
| Pocket ID OAuth | Discovery, consent, PKCE, fresh grant, refresh and revocation passed |
| Negative authorization | Wrong PKCE, reused code, invalid/missing and revoked access were rejected |
| Files | 4 MiB upload and download matched SHA-256; duplicate upload and deletion passed |
| SSE | 6-second stream, reconnect using Last-Event-ID, and 65 events over 65 seconds passed |
| Image preflight | Full isolated Linux TLS/OAuth/files/short-SSE regression ran during each build |

**DNS caveat:** the system resolver could not resolve the newly issued technical
hostname. Google DNS over HTTPS returned NXDOMAIN while Cloudflare DNS over HTTPS
returned the app's correct A record. The public transport probes therefore used
the owned app's public IP from the Timeweb API for process-local address lookup,
while retaining the original hostname for SNI and certificate validation. No hosts,
route or VPN configuration changes were made. A separate ordinary HTTPS request
still timed out in DNS resolution before cleanup. These results do not establish
that an unmodified agent can connect by hostname on that network.

An installer must gate completion on ordinary DNS and HTTPS readiness, distinguish
DNS failures from application health, and retain evidence when public resolvers
disagree. An IP override is a diagnostic aid, not a permanent user configuration.
The root cause of that DNS disagreement has not been established.

The probe intentionally obtains a new grant after its authorization-code replay
test: Pocket ID revokes tokens issued from the reused code. Reusing such a token
for a positive userinfo test would incorrectly report a provider failure.
