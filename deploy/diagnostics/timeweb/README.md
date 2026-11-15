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
