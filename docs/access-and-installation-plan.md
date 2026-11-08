# Access and installation: deferred proposal, 2026-11-07

Deferred at the owner's request: data stabilization is again release 0.8. No release is assigned to
this access proposal. Planning only; not an implemented feature list or permission to change a running installation.
The November network-change incident is owner-reported. Code inspection confirms that deployment
restricts SSH by CIDR, route repair requires elevation and rejects unowned conflicting routes,
the client currently launches stdio only, and HTTP uses one shared bearer credential.
No live network diagnosis or production change was performed for this review.

## Product decisions

Keep the owner-operated DIY model. Routine access should require neither SSH nor local elevation.
Present two placements: this computer, or the owner's server. A custom domain is an address option
for the server, not a third storage architecture. A static public IP/name and owner-paid infrastructure
are still dependencies; HTTPS does not bypass provider filtering, blocked VPN exits or corporate policy.

Distinguish remote desktop/CLI access with device credentials from browser/mobile remote connectors
with OAuth. Do not advertise browser/phone access before the latter has passed real host tests.
Teach this choice in the first access release, rather than postponing onboarding until every option exists.
Give a current cost estimate during setup; no fixed price is implied by this plan.

### A1 — address and certificate selection [deferred]

- Validate an address without buying a domain before promising it as the default. Public IP
  certificates are now available from Let's Encrypt, with a short six-day lifetime. Evaluate
  a pinned ACME client/proxy combination and test trusted validation, renewal, restart and alerts.
  Do not assume generic automatic HTTPS issues a public certificate for an IP rather than a local CA.
- Evaluate an IP-derived DNS name as an alternative. sslip.io/nip.io is an external DNS dependency;
  its operator explicitly documents issuance rate limits. It is not an unconditional scaling guarantee.
  Do not bypass CA rate limits by repeatedly switching names. Preserve ACME state across reinstalls.
- Support the owner's domain in the first access release using the same service, certificate lifecycle and client setup.
  Check DNS, address family, reachability and port availability before issuance. Dynamic DNS is an
  optional later adapter; it does not remove TLS or OAuth origin-change requirements.
- Select and document one tested default plus a recoverable fallback before implementation exit.
  For supported endpoints, report certificate expiry/renewal separately from connection failure.
  Clients must diagnose TLS failures locally even when memory_status itself is unreachable.

### A2 — HTTPS transport and service lifecycle [deferred]

- MCP Streamable HTTP behind a TLS reverse proxy; application and proxy administrative endpoints
  stay loopback-only. Expose 443 and, if needed, 80 for ACME/redirect only. The MCP client must never
  send bearer secrets or payloads to plaintext HTTP and rely on a redirect to make them private.
- HTTP should use the intended public origin, Host/Origin validation and trusted proxy settings;
  do not disable SDK DNS-rebinding protection to fix a loopback/public-host mismatch.
- Run the application with least privilege, managed restart and persistent configuration. Reuse the
  existing database, originals and backup repository. Test concurrent SSH/stdio and HTTP operations,
  SQLite locks, import resumes, backup coordination and updates stopping all relevant writers.
- Test the HTTP session's ownership by a credential, reconnects, bounded streaming and request sizes.
  Revoked credentials must not retain access through an old session, cached authorization or stream.
- Network drop during a write is not proof of failure: check operation receipts/idempotency before
  retrying. Do not automatically replay arbitrary mutations through the bridge.

### A3 — device credentials and authorization [deferred]

- Random high-entropy token per client/device; store only its verifier plus ID, label, scope,
  timestamps and revocation state on the server. Private bootstrap and client configuration writing
  deliver the secret without exposing it to agent replies, shell arguments, URLs or logs.
- Enforce read, write and administrative capabilities on tools AND resources and delivery/job paths.
  Default conversational clients do not receive credential-management, cleanup or policy authority.
  A table of every operation and its required scope is an acceptance artifact; hiding tools is insufficient.
- A revocable non-expiring device credential is an explicit usability option. Administrative grants
  should be separate and short-lived. Do not invent a recurring manual rotation burden for daily work.
- List/revoke/replace credentials without returning secrets. Offline/cloud-console recovery must work
  when all ordinary credentials are lost; a public first-visitor-wins setup endpoint is unacceptable.
- Separate workspace identity from installation/authentication identity. Define encrypted backup and
  recovery of auth state; a restored older backup must not silently resurrect revoked credentials.
  Default relocation to a different server re-enrolls device credentials unless an explicitly reviewed
  identity-preserving transfer is supported. OAuth signing keys/recovery secrets never enter public exports.

### A4 — clients and stdio bridge [deferred]

- Add a native HTTP connection profile for Portable Context itself, preserving workspace pinning and
  the existing stdio profile format. Provide a small packaged stdio-to-HTTP bridge for stdio-only clients.
  Its diagnostics use stderr; stdout remains protocol traffic. No SSH or administrative rights at runtime.
- On a requested setup, detect supported installed clients and safely merge configuration, keeping
  unrelated MCP servers and making a private rollback copy. Do not create fake configurations for absent
  clients or silently overwrite an unrelated endpoint. Check an actual memory_status after restart.
- Keep credentials in supported OS/private storage; document client-specific limitations. Configuration
  auto-edit is terminal-client functionality, not something promised from every browser assistant.

### A5 — protection, diagnostics and SSH administration [deferred]

- Bound request sizes, concurrency, timeouts and rate limits; apply both credential and network budgets.
  Failed-auth throttling must have a time window/backoff and recovery path. Ten failures must not lock
  the owner out indefinitely because a shared VPN/NAT egress also carries an attacker's traffic.
  Authenticate before expensive memory operations; never trust arbitrary forwarded-IP headers.
- Keep a bounded, redacted audit log without headers, bearer values, query credentials or transcript
  bodies. Full auth events require admin/audit permission; ordinary memory_status gives a safe summary.
  Alert on abnormal failures when possible; an unavailable endpoint also needs local diagnostics.
- Explain DNS, TCP, TLS, authentication, authorization, capture-policy and server-maintenance errors
  separately. A banner timeout alone does not prove firewall CIDR failure. Test full MCP initialization,
  not just TCP. Reports must redact configuration secrets by default.
- SSH remains a service/recovery channel with keys only. Preserve current access restrictions on
  upgrade. New-install SSH policy is an explicit choice with a cloud-console recovery procedure;
  do not silently replace a restrictive CIDR with a globally open rule.
- Changing an IP allowlist requires a reachable cloud control plane, not working SSH. Plan/review
  updates and keep the original administrative path until the replacement is verified.
- Existing helper-owned routes can be removed after successful HTTPS testing. Manual routes still
  require an exact reviewed removal; shared ownership must not be guessed. One-time OS elevation
  for removing old routes is compatible with no elevation during daily work.
- Version/security update reporting is advisory and redacted. OS unattended security updates and
  reboot behavior are an installation choice; application updates keep backup and rollback checks.

### A6 — complete local setup [deferred]

- A user-owned local folder outside the checkout and sync folders; local stdio process for installed
  compatible desktop/CLI clients. No privileged global daemon or port required.
- Provision a supported Python/runtime and dependencies without requiring users to already have
  Python 3.11, Git or an administrator-installed package manager. Pin/verify artifacts and preserve
  existing configuration. Test actual Windows ARM64/x64, macOS ARM64/Intel and Linux where supported.
- Back up to a distinct local restic repository, preferably on a separate device. Explain that a
  second directory on the same disk protects against logical mistakes, not disk loss.
- User-level Windows Task Scheduler, macOS launch agent, Linux user timer where available. Define
  behavior on sleep/logout and run missed-backup detection on next use; do not promise always-on
  execution while the device/user service is stopped. Linux without systemd needs a documented fallback.
- Fresh supported accounts get local memory, detected-client integration and a verified restore;
  absent clients are reported, not counted as passing installation tests.

### A7 — cloud-drive backups [deferred]

- Do not make a live restic repository inside arbitrary bidirectional desktop synchronization the
  default. A local repository lock and check do not prove that remote sync completed consistently.
- Prefer a supported remote backend (S3 or tested restic/rclone adapter) with actual remote verification.
  Browser/provider authorization can require user interaction; a cloud-drive folder is not automatically
  a configured backend. Add providers only after interruption/recovery and quota tests.
- A future sync-folder export must publish an immutable completed recovery set with a manifest and
  distinguish local verification from remote completion. No pruning of the last known recoverable
  set based only on a local-success result. Do not claim offline folders/files-on-demand were backed up.

### A8 — switch access versus move data [deferred]

- Adding HTTPS or an own domain on the same server changes the endpoint only: no restore or clone
  of the live database. Keep SSH clients working and switch clients one at a time with rollback.
- Moving local/server placement uses a fresh verified restic snapshot, restores into a new directory,
  verifies workspace identity, evidence tables, originals and counters, then explicitly activates it.
  Stop writes for the final cutover. Keep one authoritative writer installation; the preserved old
  directory is a recovery copy, not another active master. Reverse migration follows the same rule.
- Address changes may require client updates and OAuth reauthorization. An IP-derived address is
  not stable after an IP change; a stable DNS name is not a replicated memory or a fallback server.

### A9 — first access-release gates [deferred]

1. Existing memory is reachable from a second real network with VPN enabled without client route
   changes or elevation. Also test a second device and reboot. Do not weaken VPN kill switches.
2. HTTPS certificate is publicly trusted; renewal and restart paths work. No TLS-verification bypass.
3. Revocation denies the credential, including existing sessions/new requests, within 60 seconds.
   Read-only credentials cannot mutate through any exposed protocol operation.
4. Failed-auth tests demonstrate throttling, redacted logs and owner recovery without blanket NAT lockout.
5. Interrupted capture resumes without duplicate work; old stdio profiles and exact source revisions
   remain unchanged. Switching back to SSH is verified before removing any route or access path.
6. CI covers protocol/authorization, installer and bridge on Windows/macOS/Linux; real network/VPN
   and real hosted-client checks are recorded separately, never inferred from unit tests.

### A10 — browser/mobile authorization [deferred]

- Use a maintained OAuth/OIDC implementation integrated into the owner-operated deployment, rather
  than writing password, TOTP/passkey, token and registration protocols from scratch. Select a concrete
  implementation only after a proof of interoperability; no central developer login service is implied.
- Implement the supported MCP authorization specification: resource and authorization metadata,
  audience/resource validation, PKCE, supported registration/metadata discovery, scope consent,
  refresh/revocation and appropriate registration/redirect validation. Dynamic registration is not
  the only discovery mechanism; support must follow actual target-client compatibility.
- Owner login needs secure initial enrollment and second-factor/recovery flows. Restore and address
  changes must not accidentally reactivate revoked sessions or lose the only recovery route.
- Test Claude remote connectors from the provider's cloud infrastructure, not just the owner's browser
  network. Configure in the supported host surface, then verify mobile use. Account/workspace restrictions
  and feature availability must be checked at setup; a downloaded SKILL.md does not grant host capabilities.
- A working connector grants access to Portable Context, not automatic full export of host history or
  ongoing recording. Other web assistants are separate tested integrations, not presumed compatibility.

## Sources checked for this planning pass

- [Let's Encrypt IP/short-lived certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)
- [nip.io/sslip.io operator guidance](https://nip.io/)
- [Caddy TLS configuration](https://caddyserver.com/docs/caddyfile/directives/tls)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Claude remote custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [restic backends, including rclone](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)

These establish available mechanisms, not verified Portable Context support. Recheck exact client,
proxy and identity-provider versions during implementation. Runtime deployment guides and SKILL.md
must change with implemented capabilities, not in advance of them.
