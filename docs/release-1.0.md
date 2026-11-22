# Portable Context 1.0 — implementation and release gates

Status: in development. The memory package remains on 0.9.0 with a personal
1.0-preview HTTPS/OAuth deployment. Do not advertise
OAuth or the new HTTPS installation as released until the acceptance gates pass.

## Release decision — 2026-11-22

The public 1.0 tag is still pending. The Windows Cloud.ru installation used
`5f13ab0`; the installer and skill follow-up is `f9cf313` on
`feature/timeweb-installer` (28 of 28 CI jobs passed). On Timeweb, at
`bdf6d34`, Cursor on Ubuntu imported an ordinary
file into an existing workspace. Codex on Windows read its original bytes from
that workspace, matched the source SHA-256 and verified the unchanged second
import. See the [file receipt and both import results](timeweb-clean-test.md#результат-проверки-файла-22-ноября-2026).

| Before tagging | Evidence to collect or publish |
| --- | --- |
| Cloud.ru owner test | Separate-project provisioning and native Codex HTTPS read/write passed from Windows. The owner reports that Cursor on Ubuntu removed the test installation and started a fresh run. Its outcome is pending. Record the new installer path, quota/price review, any SSH recovery, first passkey, Cursor's own OAuth callback without manual edits, original file read-back and backup status using the [Cloud.ru acceptance steps](clean-install-test.md). |
| Close the supported-client and operations matrix | Use the [update and recovery audit](#update-and-recovery-audit-for-100-rc1) below. Record coverage per provider and client. Timeweb first-order cost/autopay review remains separate from the completed file and upgrade checks. |
| Prepare the public candidate | Bring the tested changes onto the release branch. Align package, skill and server-recommended versions, update the entry guide, compatibility list, changelog and upgrade/rollback instructions. Run the required Windows, macOS (Intel and ARM) and Linux CI on the final candidate before merging and publishing the 1.0 tag and GitHub Release. |

### Windows clean installation on Cloud.ru, 2026-11-22

The owner and Codex created a fresh memory in a separate project, using a new
private state directory. The quota usages API returned HTTP 403. The owner
checked the remaining quotas in the console; the S3 quota counter and bucket
inventory disagreed. The base estimate for this run was 962.58 RUB for 720
hours of VM, 40 GB SSD and public IPv4. Registry, HTTPS edge and usage-based
charges were outside that estimate. This was a quote, not an invoice.

The owner saw all four `PC-BOOT` checks pass in the VM console. Setup used a
temporary VM-specific route and a reviewed SSH source /32 with the VPN enabled.
After a mistyped login address, the installer issued another one-time code for
the same owner. Native Codex then read the expected workspace and an empty
project list over HTTPS/OAuth. The installation receipt recorded a verified
restic memory backup, a separate receipt confirmed the OAuth recovery kit, and
the renewal controller was installed. A restore from this particular kit and
a forced rotation on this VM were outside the run.

| Native Codex over HTTPS | First import | Independent repeat |
| --- | --- | --- |
| Added / revised / unchanged messages | 2 / 0 / 0 | 0 / 0 / 2 |
| Conversations / messages / revisions after read-back | 1 / 2 / 2 | 1 / 2 / 2 |
| File passports / linked occurrences / byte versions | 1 / 1 / 1 | 1 / 1 / 1 |
| S3 transfer result | `verified` | `previously_verified` |

The jobs had different IDs and the same package hash. Both reads matched the
original messages, roles and parent link. The file remained attached to the
first message. Both downloads of the 71-byte synthetic UTF-8 file matched its
source SHA-256, `efaf15eeb6911837a600df98571e7311869d99cc4db59e394b0be408761b697c`.
The Windows run used a native-client test helper. The follow-up commit added
`owner-page`, `ssh-recover` and `client-check`; the Ubuntu run is the first
owner acceptance of that combined path.

Private receipts used for this audit: the Windows preflight, installation,
HTTPS and recovery-kit receipts; the Timeweb `timeweb-update.json` journal and
subsequent `native-acceptance.json` read-back; the November 12 Cloud.ru HTTPS
update acceptance and read-back receipts. The public results above omit account
identifiers, private connection details and credentials.

### Local owner recovery on Windows ARM64, 2026-11-22

The source memory held a synthetic attachment, `recovery-sample.txt` (78
bytes). Its SHA-256 was recorded before backup. Separate encrypted restic
repositories held the memory and OAuth snapshots. A second local installation
used the offline recovery kit to restore the memory and verify the archived
identity in quarantine. The recovery process initialized a fresh owner with
zero passkeys. The restored database retained the source workspace ID.

In Edge, the owner enrolled a new passkey and signed in to the local test
client with it. The client used OAuth authorization code with PKCE and
`memory:read` over HTTPS. It checked the workspace with `memory_status` and
downloaded the entire attachment with `read_file_bytes`. All 78 bytes matched
the source file. The source, restored original and authenticated download
had SHA-256 `7dd505aa609409dca5c1e68d53f006a033fd904602e777643bef57058b4c738d`.

This run used native Pocket ID 2.19.2 on Windows ARM64 and a loopback HTTPS
endpoint with a certificate pinned in a dedicated Edge profile. The private
`verified-https.json` receipt ties the result to both snapshot IDs. The
Linux service cutover and cloud-provider checks have separate evidence below.

### Update and recovery audit for 1.0.0-rc.1

| Scenario | Evidence already collected | Remaining acceptance |
| --- | --- | --- |
| Upgrade an installed memory | **Live Timeweb.** The November 17 update journal records `a394ad7` → `716f3ca`, completed edge publication and committed configuration. The native client subsequently verified the same workspace, two message texts, the original file hash and wiki version. **Live Cloud.ru.** The November 12 post-update read-back verified stored content; its receipt does not identify both source commits. | Retain the new Ubuntu test workspace and its sample file if the owner approves the running cost. Once the RC commit exists, update that installation from its recorded SHA to the RC SHA. Check the same workspace, existing passkey/grant, file SHA-256 and wiki revision before and after. |
| Interrupted install or update | **Linux/systemd CI.** The [installer fixture](../deploy/smoke_https_install.py) interrupted bootstrap and updates around migration, exercised failed readiness and resumed with retained data and identity. | Keep this regression on the RC commit. An interruption of a client file upload is a separate case: interrupt a selected upload on the test installation, resume it and verify one linked original and the source SHA-256. |
| Memory and OAuth disaster recovery | **Live Cloud.ru pilot.** An isolated restore verified the OAuth snapshot and offline kit. **Linux/systemd fixture.** The [service recovery test](../deploy/smoke_recovery_installation.py) preserved originals and wiki, rejected old credentials and exercised fresh HTTPS reads/writes. **Windows owner test.** [Passkey enrollment, sign-in and file read-back](#local-owner-recovery-on-windows-arm64-2026-11-22) passed against the restored workspace. | Owner enrollment and authenticated file integrity are verified with the local test client. To claim post-recovery support for a named client, record its own reconnect and read of the retained original. |
| OAuth refresh and revocation | The [real Pocket ID fixture](../deploy/smoke_oauth_recovery.py) refreshed a token, revoked its grant and verified rejection of the old credentials. Live client sign-in and content reads have separate receipts. | For clients advertised as tested, record refresh and revoke/reconnect against a disposable grant, or label those capabilities unverified for that client. Keep the owner's working grants intact. |
| mTLS renewal after interruption | **Live Cloud.ru.** The controller resumed the same generation after an exit following a successful cloud PATCH. Authenticated access recovered and the scheduled service returned `not_due`. [Rotation evidence and transient public errors](certificate-renewal.md). | Retain the CI regression. Timeweb has controller installation/update evidence; a forced interrupted Timeweb rotation still needs a receipt if advertised as live-tested there. Continuous public availability is outside the 1.0 gate. |

**RC handoff.** Before cutting the candidate:

1. Record the independent Ubuntu/Cursor Cloud.ru installation against the
   [owner acceptance steps](clean-install-test.md), including the first file
   import, read-back from another client, repeat import and backup receipts.
2. Interrupt and resume a client file upload. Check the linked original count
   and the SHA-256 of the downloaded bytes.
3. Finish the provider/client compatibility table. Record refresh, revocation
   and reconnect using disposable grants for each client where claimed. Mark
   any pending named-client recovery, Timeweb rotation or first-order
   cost/autopay check explicitly.
4. Prepare the release branch, versions, entry guide, changelog and
   upgrade/rollback instructions.

Once the candidate commit exists, run the supported Windows, macOS (Intel and
ARM) and Linux CI matrix on that SHA. Upgrade an existing test installation
from a recorded SHA to the candidate and compare workspace, passkey/grant,
file hash and wiki revision. These checks precede the public `v1.0.0` tag.

### ChatGPT Work web plugin — existing Cloud.ru memory, 2026-11-22

The installer at `6020bf9` registered the exact redirect shown by ChatGPT on
the owner's existing Pocket ID client. The second migration reported no changes.
From the hosted ChatGPT plugin, the owner reported that `memory_status` returned
the expected workspace, `list_projects` found the expected project, and
`search_wiki` followed by `read_wiki` returned an existing page. Codex independently
matched the workspace ID, page title and quoted line through the same memory.
This verifies OAuth sign-in and cross-client reading. Write/import from ChatGPT,
Claude Cowork sign-in and the fresh Ubuntu/Cursor installation remain separate checks.

The dated sections below retain the evidence available at each earlier milestone.

### Cursor Cloud Agents OAuth

The public PKCE client `portable-context-desktop` now includes the exact
`https://www.cursor.com/agents/mcp/oauth/callback` redirect on fresh installations.
For existing memory run `python deploy/setup.py --state PRIVATE_STATE oauth-client --client cursor`
from the updated checkout. It appends missing redirects after the identity backup.
The editor still uses `.cursor/mcp.json` and `http://localhost:8787/callback`.

In the [Cursor plugins dashboard](https://cursor.com/dashboard/plugins), select **Add New MCP server → Edit JSON**,
replace the entire `my-server` template with this block, and substitute your memory URL.
Supply the four scopes explicitly: the default identity scopes omit memory access. Leave the client secret empty.

```json
{
  "portable-context-cloudru": {
    "url": "https://EXAMPLE/mcp",
    "auth": {
      "CLIENT_ID": "portable-context-desktop",
      "scopes": ["openid", "offline_access", "memory:read", "memory:write"]
    }
  }
}
```

Confirm live access by signing in again and reading a known page from the cloud agent.
See the [connection guide](../skills/portable-context/references/connection.md#cursor-cloud-agents).

## 1.0-preview.6 — Timeweb lifecycle and independent onboarding

- Timeweb records owner-reviewed costs, expected test duration and autopay status in private state before new paid orders. The same approved automatic search covers continuation on its selected VM. Replacing the app tariff requires a new cost review.
- Native Cursor Desktop setup uses a static public client and the exact localhost callback on port 8787. Existing owners can run `oauth-client --client cursor` for a checkpointed migration. The installer writes a mergeable `client-cursor.json`. Client login and ordinary-file read-back are verified separately.

- `timeweb-auto-plan` / `timeweb-auto-run`: exact approved candidate list, region/tier
  preferences, baseline hardware floor, total monthly estimate including explicit extras,
  payment-review attestation, bounded attempts and durable deadline. Sequential VM-only
  creation stops at first allocation; normal apply continues the same server. Only known
  VM POST rejections advance. Unknown outcomes, price drift and account/payment errors stop.
  Interrupted local transitions/rejections/allocation resume without duplicate creation.
  Cloud capacity and bank spending guarantees are not implied; no live paid search was run.

- Ubuntu pilot follow-up (2026-11-21): recognize exact HTTP 400
  `servers_is_run_out_error` as a reviewed VM-selection rejection, without inferring
  a quota or capacity cause. Block blind replay, retain per-attempt timestamps and
  UUID response IDs, expose next steps in status/diagnostics, and preserve old journals.
  Alternatives exclude rejected preset/zone pairs, respect preset-specific zones when
  supplied, and explicitly distinguish catalog membership from actual orderability.
  No paid live retry was performed for this correction.

- Timeweb hardware-tier fallback after definitive capacity rejection, explicit zones,
  separately journaled public IPv4, secret-masking detection, dependency readiness,
  and bounded ordinary DNS/HTTPS waiting.
- `timeweb-update`: reviewed pinned gateway upgrade, coordinated with the VM renewal
  controller; durable publication intent, same workspace/resources, resumable memory update.
- `timeweb-diagnostics`: allowlisted support JSON without provider bodies, environment,
  credentials or memory contents; works during interrupted local configuration transitions.
- `timeweb-cleanup`: installation-scoped plan, irreversible S3-backup warning, deletion
  receipts, retained IPv4 cleanup, shared metadata protection and retired-state guard.
- Skill revision 1.0-preview.6, [Timeweb operations](timeweb-operations.md), and a
  [clean Cursor starting prompt](timeweb-clean-test.md) that needs no developer chat context.
- Infrastructure was exercised on the earlier Timeweb pilot. The November 22
  result above records subsequent Cursor file acceptance on an existing installation.

## 1.0-preview.5 — interrupted Ubuntu/Cursor install

- Exact Container Apps name-not-found HTTP 499 handling at the pre-create lookup;
  unknown 499 and ambiguous CREATE replies do not authorize another resource.
- Bounded GET/SSH read retries, short visibility grace for owned newly created
  resources, fixed safe error categories and local stage/status reporting.
- Same-source HTTPS resume verifies live workspace/release and a full installer
  fingerprint before skipping the memory migration/backup restore exercise.
  A changed source still uses the normal backed-up package update.
- Network diagnostics include private registry data-plane hosts, cloud control
  APIs, TLS/HTTP, missing owned routes and Linux policy routing. Cloud 443 and
  VM SSH paths are diagnosed separately; no global VPN routing assumption.
- Cloud VM readiness checks clock/dependencies and permits explicit acceptance
  of reviewed historical cloud-init errors only for the same status/config/boot.
- Quota discovery reads project/ancestor usage via Evolution v3, with numerical
  comparison for reviewed API-code demands. Missing permissions/schema/data are
  unknown; default limits are never substituted. Preflight without demands is
  review-required, not an automatic guarantee or a physical capacity check.
- [Resume guide](install-resume.md) and skill revision 1.0-preview.5 preserve the
  existing installation. The actual second-memory Cursor OAuth pass is still a
  release gate; mock API tests do not establish live provider compatibility.

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

## Agent-guided onboarding — 2026-11-11

The repository now starts at [START-HERE](../START-HERE.md). The skill routes first contact
through existing/new/unsure memory, host capabilities, Cloud.ru HTTPS or own-server/local
setup, real client login, recovery handoff, chosen first import/read-back and a second agent.
Commands, transport limits and receipt paths are checked against this checkout. The local
client guide preserves personal notes and distinguishes client preparation from a verified
SSH/stdio connection. MCP-only setup/review resources follow the same workflow.

This is documentation and local validation, not completion of the fresh Cloud.ru user journey.
Before publication, walk the following acceptance scenarios using these instructions, record
client/OS/version, chosen ref and evidence, and keep only non-secret results in the release notes:

| Scenario | Required observed result |
|---|---|
| New Cloud.ru owner | Reviewed cost plan, one installation, provider HTTPS, browser/passkey login, same workspace, backup/rotation receipts |
| Existing or uncertain memory | Find the actual endpoint; no replacement on empty state or connection failure |
| Own Linux server / local folder | Reach existing directory via supported transport; clearly state off-server backup status |
| First selected import | Correct roles/IDs, originals and file passports, read-back, interrupted upload resume, unchanged reimport |
| Wiki and second agent | Evidence-linked pages, honest review coverage, same project/file recalled by the second client |
| Pause / failed update / lost passkey | Same state/package resumes; private owner login works; recovery enrollment verified in browser |
| Web-only or incompatible OAuth host | Explain missing capability and offer a concrete supported route without claiming connection |

No new paid Cloud.ru installation was created for this documentation pass. Full native HTTPS
capture/client acceptance remains separate from protocol/installer tests. Version/tag publication
and marketplace packaging are not implied by the new entry page.

## Existing-owner Claude Code pilot — 2026-11-11

The owner reported successful browser authorization and project reads with Claude Code 2.1.321
against the existing HTTPS memory over VPN, without an SSH bypass route. Access had read/write
scopes but no memory:admin. Codex independently returned memory_status over its existing OAuth
connection after the correction. This verifies login and recall, not Claude Code writes, file
capture, refresh, revocation or the full new-owner journey.

The blocker was an exact redirect URI mismatch: Claude Code uses localhost while Codex uses
127.0.0.1. The owner installation was corrected through Pocket ID's UI, preserving the original
callback/security settings. New bootstrap registrations include both exact loopback URLs.
For existing installations, `setup.py oauth-client --client claude-code` provides an explicit
checkpointed SSH migration; the connection guide also documents the HTTPS UI route.

The skill now reports revision 1.0-preview.2 separately from package baseline 0.9.0. A pre-login
`claude mcp get` timeout is documented as inconclusive, not as proof of server failure or proof
that authentication is the only possible cause. Full upstream CLI error wording is outside this fix.

## Second-installation isolation review — 2026-11-12

The next owner acceptance uses a separate Cloud.ru project in the same account,
a fresh private state directory and Cursor Desktop on another computer. Start at
[the clean test guide](clean-install-test.md), using a pinned checkout. No second
cloud installation was created by this review; Cursor OAuth remains experimental.

Local two-installation tests exercise unique deployment/bucket names, separate
rendered Terraform variables, workspace IDs and journals, and preservation of the
first installation's files. Existing recovery/HTTPS checks bind the workspace and
issuer; S3 permissions target the installation's bucket, with per-workspace restic
repositories. Separate VMs isolate the default service names, paths and ports.

The review added guards against configure retargeting a used state directory,
inherited Terraform data/workspace/argument/variable overrides, and cross-project
HTTPS/API requests. Same-target SSH CIDR changes and normal configuration updates
remain available. These guard accidental use through the supported installer;
they are not an authorization boundary against manual state edits or account-wide
cloud credentials. Use project-scoped credentials, not a copied production kit.

Skill revision 1.0-preview.4 adds the additional-memory route and documented Cursor
static OAuth setup. The existing installation was not upgraded by this review.
Two memories on one VM/user and two installations in one Cloud.ru project are
outside this acceptance scenario. Live creation, login, file capture and recall
are still required before declaring the new-user journey complete.

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
