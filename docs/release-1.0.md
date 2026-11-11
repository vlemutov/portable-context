# Portable Context 1.0 — implementation and release gates

Status: in development. The memory package remains on 0.9.0 with a personal
1.0-preview HTTPS/OAuth deployment. Do not advertise
OAuth or the new HTTPS installation as released until the acceptance gates pass.

OAuth resource-server code and per-operation capabilities are now implemented in
the branch; see [the OAuth preview contract](oauth-preview.md). The personal
pilot completed owner passkey enrollment, native Codex OAuth login, and project
create/update/read/archive over HTTPS on 2026-11-10.

## Recovery acceptance (2026-11-10)

- Separate encrypted restic repository for identity state is installed on the
  pilot VM. The first S3 snapshot and full isolated restore passed (nine files).
  Identity backup runs every six hours; full restore verification and retention
  run weekly. Memory backup schedules remain separate.
- `deploy/oauth_recovery.py` provides private owner login codes, backup/check,
  quarantined restore, fresh identity bootstrap and explicit existing-server
  activation. [Recovery instructions](oauth-recovery.md) explain the owner kit,
  lost-device revocation and fresh login on every device after disaster recovery.
- The isolated real-provider test took a snapshot before revocation, then
  revoked access and restored the older snapshot through the recovery path.
  The fresh provider rejected old access/refresh tokens and browser cookies,
  generated different signing keys, omitted a deleted historical passkey record,
  and accepted the new owner's one-time login.
- A complete disposable installation passed HTTPS writes, loss of the entire
  active directory, restoration from the offline kit, real systemd cutover,
  fresh consent, old-token rejection and new HTTPS reads/writes. Originals and
  a manual wiki file survived. This also passed the Linux GitHub CI job.
  Live identity replacement was not performed on the owner's working memory;
  physical passkey enrollment remains a separate browser acceptance check.
- The installer handed off a verified offline kit on the pilot over SSH;
  its second run reused the kit. Credentials never appeared in command output.
- An MCP restart exposed a readiness gap: systemd can report `active` before MCP
  accepts connections. Recovery activation now waits for protected-resource
  metadata. General updates still need coordinated readiness and client retry.

## Agreed deployment

The owner does not have to buy a domain. For Cloud.ru, evaluate its managed
Container Apps HTTPS address with a **stateless edge only**. Keep the existing VM,
SQLite, originals, import jobs, wiki, backup schedules and restic repository.
Own-domain and own-server installation remain supported targets. No shared SaaS.

```
Agent -- public HTTPS --> Cloud.ru Container Apps / Caddy
      -- mutually authenticated TLS --> Caddy on owner VM
      -- loopback --> authenticated MCP / identity service
                         | SQLite + jobs on VM
                         | S3 originals + restic backups
```

The edge certificate authenticates a machine, not the user. OAuth/device
authorization remains mandatory at the application. Do not expose unprotected
MCP while implementing this transport. Probe the gateway with synthetic data first.

## Edge implementation

- `deploy/gateway.py`: validate destinations, generate separate server/client
  trust roots, render Caddy configuration. Never disable TLS verification.
- `deploy/cloud_gateway.py`: official Cloud.ru API adapter and bounded container
  request. References a pinned owner-registry image and a Secret Management key.
- `deploy/edge`: pinned upstream Caddy image, unprivileged entrypoint, no volumes
  or memory in the image; secrets materialize only in temporary runtime files.
- Origin and edge check the public Host; SNI and Host match. Drop untrusted
  forwarded headers; do not infer a real user's IP from provider forwarding yet.
- 16 MB request limit (existing captures use smaller resumable chunks), no proxy
  retry of failed mutations, unbuffered responses, 600 s provider request limit.
  Durable imports must outlive HTTP disconnects on the VM, not in the edge.
- Minimum zero / maximum one replica, 60 s idle timeout, 0.1 vCPU / 256 MiB initial
  candidate. Validate actual provider minima and workload before claiming costs.
- No request/access logs containing authorization, cookies, query strings or
  transcripts. Health probe reports only process liveness, not memory readiness.

Private key generations have at most 90-day validity. **Automatic rotation and
restart/overlap recovery are implemented in the journaled rotation controller**;
see [certificate renewal](certificate-renewal.md). Real Caddy tests kill the
controller at every stage while requests continue. Cloud publication has its own
live acceptance; `gateway.py prepare` alone still does not install renewal.
Never overwrite a live key generation during a repeated installation.

## Prototype evidence (2026-11-10)

- Eight gateway tests passed on native Windows ARM64 and on the Linux VM with
  real Caddy processes. GitHub Actions also passed on Linux x64/ARM64, Windows
  x64, macOS Intel and macOS ARM64 at commit `181996d`.
- Existing deployment CI and the complete Python test matrix passed at that commit.
- The gateway, deployment and Python CI suites also passed at `a56bbfa` after
  incorporating the observed Cloud.ru API and revision-publication constraints.
- A real Cloud.ru HTTPS request reached the synthetic VM origin through mTLS.
  Direct connections to the origin without the client certificate were rejected.
  The production memory was not connected to that probe.
- The provider URL survived replacement of the failed stateless prototype and a
  subsequent normal revision update. A 1,100,988-byte binary payload round-tripped
  exactly; three SSE events arrived separately at approximately 0.1/1.1/2.1 s.
  The temporary diagnostic container was deleted; one main edge remains.
- Provider `publicUri` is a bare hostname; normalize to HTTPS and validate it.
  Startup arguments cannot contain literal newlines. Secret references must be
  root-level names; configuration contains a reference, never the private key.
- A custom HTTP liveness probe accepted by the API left publication in `creating`
  / `for_patch`. The same runtime without that probe published successfully.
  Omit custom probes until their full contract is verified; the `/_pc/health`
  endpoint remains available for installer diagnostics. Do not claim a precise
  provider root cause from this isolation test alone.
- The provider overwrites `metadata.createdByProduct`; retain resource IDs from
  creation receipts for ownership checks. Never delete by a guessed name alone.
- A firewall rule reporting `CREATED` did not immediately pass traffic. Probe
  the actual authenticated connection before declaring networking ready.

## Identity-provider experiment (not production integration)

Pocket ID v2.19.2 was exercised on the existing Linux VM in a separate loopback
lab, with synthetic users, clients, grants and its own database. The downloaded
Linux amd64 binary was checksum-verified. Both the provider and a local Caddy
HTTPS listener were stopped after each experiment. Neither was routed through
the public edge or connected to the memory database.

Observed with a registered public client and an explicit API resource:

- S256 PKCE authorization-code exchange produced access and refresh tokens.
- Refresh succeeded; OIDC userinfo returned the expected synthetic owner.
- Revoking that user's client authorization immediately made userinfo return
  HTTP 401 and refresh return HTTP 400 `invalid_grant`.
- Self-introspection using the access token as bearer authentication returned
  HTTP 401 `request_unauthorized`. Do not assume this endpoint is available to
  the MCP resource server merely because it appears in discovery.
- Userinfo can be investigated as an additional online revocation check when
  `openid` and the provider audience are granted. It cannot replace validation
  of the MCP token's signature, issuer, API audience, expiry, subject and scopes.
  An unavailable provider must not silently turn into an accepted token.
- The local test must use HTTPS: the provider sets secure session cookies even
  with an HTTP localhost application URL. The experiment trusted its generated
  localhost certificate explicitly; TLS verification was not disabled.

This section records the early experiment. Subsequent preview tests verified
invalid PKCE/code replay rejection, per-operation capabilities, recovery and
revocation; the personal Codex browser connection also passed. It is not evidence
of compatibility with every other client. Synthetic setup used a private API
key and one-time login; production must not expose a reusable bootstrap key or
rely on the first public visitor becoming the owner.

Provider references used for this experiment:
- https://github.com/pocket-id/pocket-id/tree/v2.19.2
- https://pocket-id.org/docs/guides/client-id-metadata-documents
- https://pocket-id.org/docs/configuration/environment-variables

## Remaining 1.0 gates — scope reset, 2026-11-10

The owner explicitly deferred further investigation of isolated Cloud.ru public
errors. Zero-downtime cloud publication is NOT a 1.0 release gate. Retain the
measured limitations in [certificate renewal](certificate-renewal.md), working
automatic renewal/recovery and safe diagnostics. Keep min/max instances at 0/1;
do not enable paid warm instances as part of release preparation.

The 1.0 goal is a reproducible owner-operated product: a new user can start from
the repository with an assistant, connect or create memory, authenticate, import
and recall it from another supported client, update it, and recover access/data.
Remaining work is ordered as follows:

1. One reproducible HTTPS/OAuth install/update flow, replacing pilot-only scripts:
   private owner enrollment, client configuration, recovery kit, coordinated service
   stop/start and actual readiness, safe retry of interrupted operations, and useful
   diagnostics. Exercise a clean installation, an existing-memory upgrade and a
   network change from supported Windows/macOS/Linux clients. Explain replacement-VM
   recovery and actual Cloud.ru costs, including registry and secrets.
2. Public client compatibility and an actual cross-agent memory journey: selected
   capture with files over HTTPS, interruption/resume, duplicate-free reimport,
   cited wiki enrichment, then recall by another supported agent. Validate actual
   callback/registration, consent, refresh and revocation for each advertised client.
   Start with Codex and Claude Code as the target tested pair, not every agent or
   web client. Add a browser check for fresh passkey enrollment after recovery;
   protocol tests do not substitute for it. Publish tested capabilities and gaps;
   untested clients are experimental, not blockers for the supported pair.
3. User-facing onboarding and recovery: refresh the skill and entry guide around
   existing vs new memory, cost review, owner login, first import and second-client
   connection. Integrate the already-tested private recovery kit and restore flow;
   complete fresh passkey enrollment after recovery and explain replacement-VM
   recovery. Normal HTTPS use must not require recurring SSH/firewall adjustments;
   infrastructure administration may still need the documented recovery path.
4. Release packaging and final regression: consistent 1.0 versions and skill recommendations, current
   START-HERE/onboarding instructions, supported-platform/client matrix, changelog,
   pinned release artifacts and upgrade/rollback instructions. All required CI
   checks for the final supported Windows/macOS/Linux matrix must pass. Merge/tag
   only after these scoped acceptance gates pass; keep SSH recovery available.

## Installer implementation and acceptance — 2026-11-11

The CLI now joins the existing VM/memory installer to the HTTPS/OAuth lifecycle:

```text
python deploy/setup.py --state PRIVATE_DIRECTORY https-plan
python deploy/setup.py --state PRIVATE_DIRECTORY install --https --approve-plan REVIEWED_HASH
python deploy/setup.py --state PRIVATE_DIRECTORY update
python deploy/setup.py --state PRIVATE_DIRECTORY https-check
python deploy/setup.py --state PRIVATE_DIRECTORY owner-login
```

The base VM must already have been prepared/applied using the existing reviewed
flow. The HTTPS plan adds private registry/image storage, the stateless Container
Apps endpoint, its transport secret and the narrow mTLS ingress rule. Scaling
remains 0/1. No quoted price or zero-downtime guarantee is implied. Automatic
Pocket ID provisioning currently requires a Linux AMD64 server; the local CLI
and SSH memory installer have separate cross-platform coverage.

Keep the same private state directory when retrying. Journaled resource IDs and
exact plan receipts prevent a reconnect from creating a second installation.
An ambiguous creation response requires an explicit matching resource ID through
`https-reconcile --stage registry|container --resource-id REVIEWED_ID`; it does
not trigger another CREATE. An existing unrelated application is not accepted
as the setup placeholder. The first login code is handed off through a private
local file; updates do not issue a new login or replace identity/transport keys.

Updates checkpoint data, stop HTTP/SSH writers, use the candidate package for
migration, verify restoration, and switch `current` before reopening access.
An interrupted migration must resume with the same package, not roll old code
back over the database. Failed HTTP readiness leaves a maintenance gate and
stops the HTTP service. Existing pilot installations are recognized by their
workspace, issuer, owner binding and owned units; connection commands can adopt
their verified endpoint without provisioning another account or container.

Acceptance uses separate Linux jobs for identity recovery, certificate rotation
and HTTPS installation. The installer fixture uses real systemd, Pocket ID,
Caddy, restic and HTTPS/OAuth, with synthetic memory and no cloud account. It
checks interrupted bootstrap, interrupted updates before/after migration,
readiness failure, unchanged identity/grants, retained originals/wiki and
duplicate-free reimport. Cloud API fault tests separately exercise lost replies
and resource reconciliation. These do not substitute for the upcoming fresh
Cloud.ru owner journey or physical passkey enrollment in supported clients.

The rotation acceptance records transient read-probe disconnects separately
from protocol/authentication or integrity failures. Successful recovery, valid
responses, the full existing stream, exactly one long mutation and rejection of
retired certificates remain assertions. `smoke_gateway_rotation.py
--require-continuity` retains the optional stricter experiment; zero disconnects
are not silently assumed or required by the scoped 1.0 gate.

## After 1.0

- Investigate isolated provider/public-network 503/504/TLS failures, optionally
  using independent public observers and current provider ingress events.
- Evaluate zero-downtime cloud revisions, warm instances and other availability
  options only as separately scoped work with cost review; no availability guarantee.
- Expand actual OAuth/client coverage, web clients and marketplace packaging.
  Do not require all originally named agents to pass before shipping the tested subset.
- Broader provider optimization, high availability and new memory features are
  not part of this release's final stretch.

Transient connection failures remain a documented limitation, not permission to
lose data, bypass authentication or blindly repeat writes. Existing durable jobs
must remain resumable; reproducible data-loss, authorization and recovery defects
remain release blockers.

Already closed: basic HTTPS/mTLS transport (stable provider URL, streaming and
approximately 1 MiB binary probe), Codex OAuth read/write, separate S3 identity
backup/restore, fresh identity recovery, disposable full service cutover and
private installer kit handoff. These need regression coverage, not reinvention.

Sources checked 2026-11-09:
- https://cloud.ru/docs/container-apps-evolution/ug/topics/guides__container-create
- https://cloud.ru/docs/container-apps-evolution/ug/topics/api-ref
- https://cloud.ru/docs/container-apps-evolution/ug/topics/concepts__volumes
- https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- https://caddyserver.com/docs/caddyfile/directives/tls
