# OAuth access — 1.0 preview

This branch implements the MCP resource server, not a new OAuth issuer. Pocket ID
v2.19.2 runs on the owner's Linux VM. The Cloud.ru container remains a stateless
HTTPS proxy. Do not describe this preview as the complete 1.0 installer/release.

## Connection and permissions

The endpoint is `https://YOUR-ORIGIN/mcp`; the issuer is `https://YOUR-ORIGIN`
with **no trailing slash**. Discovery is served at
`/.well-known/oauth-protected-resource/mcp` and
`/.well-known/openid-configuration`. The authentication challenge advertises
`openid memory:read` as the initial minimum. Supported scopes also include
`offline_access`, `memory:write` and `memory:admin`.

| Requested access | Required scopes | Effect |
|---|---|---|
| Read | `openid memory:read` | Recall imported messages/files/wiki, inspect projects, jobs and reports |
| Write | Above plus `memory:write` | Upload selected input, import staged host records, organize projects, enrich wiki |
| Admin | Above plus `memory:admin` | Change policies, initiate server-filesystem imports, cleanup, explicit backup/S3 maintenance |

Permissions are enforced before every tool/resource operation. Unknown methods,
tools and resources fail closed. The executable inventory is
`src/portable_context/permissions.py`; a test compares it with every registered
tool so a new tool cannot silently inherit access. Project groups are not access
boundaries: this remains one owner's memory, not a multi-user service.

`store_attachment(server_path=...)` requires admin even though uploading bytes
requires only write. Registering/replacing a server-local source requires admin.
Running a staged host-data import requires write; running other import jobs
requires admin. A read client cannot obtain write access by changing arguments.
Scope strings are explicit: an admin client also requests read/write as needed.

The HTTP server validates signature, token type, issuer, audience, expiry, owner
subject, client identity and scopes. It then checks Pocket ID userinfo online;
this profile requires `openid`. No successful token-validation result is cached.
Checks run both at HTTP authentication and at operation dispatch. Public signing
keys alone are cached with bounded refresh. An unavailable provider denies access.
Revocation stops subsequent operations, including those on an already-open client
connection; it does not undo an operation committed before revocation.

HTTP is stateless. Durable import/enrichment jobs remain in SQLite and can resume
after reconnection. A disconnected mutation must be checked by receipt/request ID
before retry. Local stdio remains an owner-operated channel with existing rights;
OAuth does not restrict processes that already have filesystem access to the store.

## Private server configuration

Install the `mcp` extra. Configure the owner before exposing the public route:

```json
{
  "issuer": "https://YOUR-ORIGIN",
  "resource": "https://YOUR-ORIGIN/mcp",
  "provider_origin": "http://127.0.0.1:14120",
  "owner_subject": "OWNER-ID-FROM-PRIVATE-BOOTSTRAP"
}
```

```sh
portable-context --home /PRIVATE/memory mcp --transport streamable-http \
  --port 8767 --oauth-config /PRIVATE/oauth.json
```

Only the loopback provider may use HTTP; public connections require HTTPS.
Neither tokens nor provider keys belong in command arguments or this file. The
The HTTP process needs the same attachment/backup configuration and maintenance lock
as stdio. Use the current `deploy/setup.py --state PRIVATE_STATE update`: it coordinates
service quiescence, checkpoint, migration, readiness and reopening. Do not use older deployment
tooling or manually clear a failed maintenance gate. Resume the same package and private state.
The main user route is [onboarding](../skills/portable-context/references/onboarding.md).

`deploy/pocketid.py` bootstraps a provider through loopback with a private API key.
Retain its receipt outside the repository; it pins the owner and API identities
and preserves later grant choices. In this provider version, application settings
from environment variables require `UI_CONFIG_DISABLED=true`; merely setting
`REQUIRE_USER_EMAIL=false` is insufficient with UI-managed configuration enabled.
Disable open signup and create the owner before public routing. The gateway also
blocks public initial signup and strips `X-API-Key` before forwarding.

For first owner enrollment, use `Bootstrap.create_owner_login_code(owner_subject)`
through private loopback and show the result only in a private local handoff.
Pocket ID v2.19.2 returns a six-character code for its default 15-minute TTL,
while its login form requires twelve characters when anonymous email login is
disabled. The helper requests a 20-minute code and validates its format before
handoff. Keep the code out of URLs, logs, chat and Git. Browser enrollment must
be checked separately from API token exchange: protocol tests alone miss this UI
constraint. Expired codes require a new private handoff.

Bootstrap creates separate public PKCE clients `portable-context-desktop` and
`portable-context-admin`. The desktop client can request read/write only. Both
require consent and use a 10-minute access-token lifetime. The admin client uses
`http://127.0.0.1:14881/callback`. New desktop clients also register the exact
`http://localhost:14881/callback` for Claude Code, alongside Codex's 127.0.0.1 form.
Existing installations opt in through `setup.py --state PRIVATE_STATE oauth-client --client claude-code`;
this checkpoints identity and preserves existing grants/settings. Updating the skill alone
does not change provider registration. See the [connection guide](../skills/portable-context/references/connection.md).
Do not reuse one client ID for multiple devices
if independent device revocation is needed: create a separate client per device.
Pocket ID's Authorized Apps screen revokes a user's authorization for a client.

## Codex connection (verified personal pilot)

Use a separate name while testing; retain the old connection until recall works.
The configuration below contains no secrets:

```toml
[mcp_servers.portable_context_https]
url = "https://YOUR-ORIGIN/mcp"
scopes = ["openid", "offline_access", "memory:read", "memory:write"]
startup_timeout_sec = 60
tool_timeout_sec = 180

[mcp_servers.portable_context_https.oauth]
client_id = "portable-context-desktop"
callback_port = 14881
callback_url = "http://127.0.0.1:14881/callback"
```

Run `codex mcp login portable_context_https`, sign in to your own provider and
review the requested permissions. Tokens belong in the client's credential store,
not chat or Git. Check `memory_status.workspace_id` against the existing memory.
Do not mark a client supported until this actual browser flow and tool calls pass.

Other clients may use exact allowlisted Client ID Metadata Documents or a
pre-registered client with its actual callback. Do not add wildcard callbacks or
an unrestricted metadata URL allowlist to make registration errors disappear.
The generic bootstrap does not enable CIMD or dynamic registration by default.

## Evidence and remaining work

Local tests cover JWT rejection, owner/audience binding, permissions for every
tool, HTTP discovery, safe initial scope selection and immediate revocation.
Real Caddy tests cover identity/MCP routing, mTLS, stripping bootstrap keys and
blocking public initial setup. A separate VM experiment with actual Pocket ID
tokens passed S256 exchange, read MCP calls, denied write, refresh, revoked-token
rejection, rejected code replay and wrong-PKCE rejection.

The personal Codex pilot completed browser/passkey enrollment and native HTTPS
read/write tool calls on 2026-11-10. Project create/update/read/archive passed;
this does not certify capture uploads or other hosts. Recovery commands and
separate encrypted identity backups are described in [OAuth recovery](oauth-recovery.md).

Recovery was exercised against real Pocket ID and restic, including a snapshot
taken before revocation, new signing keys, and rejection of old sessions after
recovery. A separate S3 identity backup and full restore passed on the pilot VM.

Disposable full service cutover and installer kit handoff have also passed. Certificate rotation
and interrupted-operation recovery, managed setup/update and migration readiness gates now have
automated checks, including real Linux services. This does not promise zero downtime during a
Cloud.ru container publication. See [release gates](release-1.0.md) for evidence and remaining work.

Still validate the complete fresh-account onboarding, first selected capture/wiki flow through
the intended client, browser recovery enrollment, and each additional client before claiming
support. The [connection guide](../skills/portable-context/references/connection.md) records the
current limits; the capture CLI remains SSH/stdio even when the agent's MCP uses HTTPS.

References checked 2026-11-10:
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Pocket ID metadata clients](https://pocket-id.org/docs/guides/client-id-metadata-documents)
- [OpenAI OAuth integration](https://developers.openai.com/plugins/build/auth)
- [Codex OAuth configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
