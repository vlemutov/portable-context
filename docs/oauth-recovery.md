# OAuth recovery and backups (1.0 preview)

Memory recovery and access recovery are different operations. Losing a passkey
does not require restoring memory. Restoring an old identity database directly
can revive a deleted passkey, consent or session. Portable Context therefore
keeps the old identity archive in quarantine and creates a **fresh identity** for
disaster recovery. All devices enroll/sign in again. Memory and its project IDs
stay unchanged.

## What the owner keeps outside the server

- Access to the cloud account / server console, or the private SSH deployment kit.
- The restic recovery password in a password manager or another offline location.
- The private backup configuration and access to its S3 bucket. A password alone
  does not locate the repository or grant cloud access.
- Preferably a second passkey on another device. This avoids emergency recovery
  if just one device is lost.

The existing installer saves the restic password in the private deployment
directory. OAuth uses that same password in a **separate restic repository**. Do
not paste recovery files, login codes, keys or tokens into an agent chat or Git.
The recovery authority is control of the owner's server/cloud account, not a
public "reset password" endpoint.

The local installer now checks for an existing OAuth runtime after `install`.
When present, it configures a separate identity repository, verifies a full
restore, installs the backup schedule, and saves the kit privately on the owner's
computer. If OAuth has not been installed yet, the result explicitly says
`oauth_not_installed`; it does not claim that access recovery is ready.
After setting up OAuth (or rotating S3 credentials), run on your computer:

```sh
python deploy/setup.py --state /PRIVATE/deployment recovery-kit
```

The command prints only the local path and a verification receipt. No manual
SFTP copying is required. It detects `oauth` or the pilot's `oauth10` runtime;
use `--runtime /SERVER/ROOT/oauth` if both exist. An unchanged kit is reused.
Changed credentials go into a new directory, preserving the old kit. An explicit
`--output /PRIVATE/new-kit` must be new or contain the same verified kit.
Keep another protected copy outside both server and computer.

For manual server administration, the lower-level export remains available:

```sh
python deploy/oauth_recovery.py export-kit --config /PRIVATE/identity-backup-config.json \
  --output /PRIVATE/new-recovery-kit
```

It contains both repository addresses, memory workspace ID, password and S3 credentials. **It is secret,
not an attachment for an agent.** After server loss, opening the existing archive
does not require the old runtime or its disks:

```sh
python deploy/oauth_recovery.py restore-config --kit /PRIVATE/recovery-kit \
  --config /NEW/identity-backup-config.json --runtime /NEW/oauth-runtime \
  --memory-home /NEW/memory --binary /TOOLS/restic --expected-origin https://YOUR-ORIGIN
```

Then use `list` and `restore` below. Do not run `init` on the recovered repository.
Obtain the pinned Pocket ID/restic binaries separately. The kit grants backup
access; the cloud account or SSH/console access is still needed to replace services.

To reopen memory backups from the same kit after losing all server files:

```sh
python deploy/oauth_recovery.py restore-memory-config --kit /PRIVATE/recovery-kit \
  --config /NEW/restic-config.json --memory-home /NEW/memory \
  --binary /TOOLS/restic --expected-origin https://YOUR-ORIGIN
python -m portable_context.restic_backup --home /NEW/memory --config /NEW/restic-config.json list
```

Restore the selected memory snapshot to a new directory using that configuration,
check its workspace ID against `kit.json`, and activate it with MCP stopped.
Old kits without memory metadata still restore OAuth; retrieve a new kit before
depending on one package for both repositories.

## Lost passkey; server still available

For a managed installation, use the local checkout and the same private deployment state:

```sh
python deploy/setup.py --state /PRIVATE/deployment owner-login
```

This retrieves a fresh private login handoff onto the owner's computer over SSH. It prints only
the path and non-secret receipt. The owner opens the file privately, follows `login_url`, enters
the 12-character code and creates a new passkey. The code expires after 20 minutes; rerun this
command if needed. Do not paste it into chat. No new memory or cloud resources are created.

For manual administration without the local installer state, use the server's Python environment
and an owner SSH/console session:

```sh
python deploy/oauth_recovery.py owner-login \
  --runtime /PRIVATE/oauth-runtime --output /PRIVATE/new-owner-login.json
```

The helper verifies the pinned runtime, owner and issuer, and writes a 12-character
code to a new private file. It prints only the file path. The owner retrieves the
file privately (for example SFTP), opens its `login_url`, enters the code and adds
a new passkey. The code lasts 20 minutes. Delete the handoff file after use.

Adding a passkey does **not** revoke a lost device. In account settings remove
lost passkeys and revoke its authorized applications/sessions. The preview's
shared desktop client ID revokes all authorizations for that client, so reconnect
the remaining devices. Independent device clients belong to installer work.

## Configure backups

Run on the Linux memory server with the installed Python dependencies. Configuration
and staging directories must be private and outside the public repository.

```sh
python deploy/oauth_recovery.py configure \
  --runtime /PRIVATE/oauth-runtime \
  --memory-config /PRIVATE/restic-config.json \
  --config /PRIVATE/identity-backup-config.json \
  --repository 's3:https://ENDPOINT/BUCKET/restic/WORKSPACE-identity'
python deploy/oauth_recovery.py init --config /PRIVATE/identity-backup-config.json
python deploy/oauth_recovery.py backup --config /PRIVATE/identity-backup-config.json
python deploy/oauth_recovery.py check --config /PRIVATE/identity-backup-config.json
python deploy/oauth_recovery.py schedule --config /PRIVATE/identity-backup-config.json
```

`init` is an explicit first-time operation. Never interpret a failed repository
connection as permission to initialize or replace an existing repository.
`configure` is repeatable only with identical settings. The OAuth repository must
be separate from, and not nested inside, the memory repository.

Copies contain a consistent SQLite snapshot (including committed WAL changes),
local identity data, the encryption/bootstrap keys, owner receipt and MCP OAuth
configuration. Restic encrypts them before S3 delivery. Temporary plaintext
staging is private and removed after the attempt. Interrupted restores remain
private and cannot be prepared for activation. A hard process/OS crash can leave
private staging behind; remove abandoned staging after verifying no job is running.

The current profile supports pinned Pocket ID 2.19.2, local SQLite and local
identity files. PostgreSQL, external actor stores and external key-file profiles
are rejected instead of silently producing incomplete backups.

The schedule makes a copy every six hours, verifies a full restore weekly and
prunes weekly (server timezone). Retention defaults to 14 latest, 8 weekly and 12
monthly snapshots. Existing memory schedules and repository are untouched. S3
historical object versions have their own lifecycle. Login activity can change
the identity database and therefore create a new snapshot even if memory did not
change. `status`, `list` and `check` return no credentials; `backup_status` in MCP
also exposes only redacted identity backup receipts for the standard deployment.

## Server or identity disaster recovery

1. Recover memory separately if necessary. Keep the HTTPS MCP service stopped.
2. Restore an OAuth archive into a **new quarantine directory**:

   ```sh
   python deploy/oauth_recovery.py list --config /PRIVATE/identity-backup-config.json
   python deploy/oauth_recovery.py restore --config /PRIVATE/identity-backup-config.json \
     --snapshot FULL_SNAPSHOT_ID --target /PRIVATE/quarantine
   ```

3. Prepare a fresh provider using a separately obtained, checksum-verified binary:

   ```sh
   python deploy/oauth_recovery.py prepare-recovery \
     --quarantine /PRIVATE/quarantine --target /PRIVATE/recovered-runtime \
     --binary /PRIVATE/pocket-id --expected-origin https://YOUR-ORIGIN --port 14130
   python deploy/oauth_recovery.py bootstrap-recovery --target /PRIVATE/recovered-runtime
   ```

   The temporary bootstrap binds only loopback and stops when finished. It creates
   new signing/encryption/bootstrap keys, a new pinned owner, zero passkeys and
   new default public desktop/admin client registrations. It does not import the
   historical database, grants, sessions, API credentials or custom clients. A
   failed bootstrap must be retried in a new directory. Re-establish any custom
   clients explicitly after recovery; they are not silently restored.

4. On the standard existing Linux/systemd installation, explicitly activate:

   ```sh
   python deploy/oauth_recovery.py activate-recovery \
     --target /PRIVATE/recovered-runtime --config /PRIVATE/identity-backup-config.json \
     --expected-owner CURRENT_OWNER_SUBJECT --confirm 'RESET ACCESS https://YOUR-ORIGIN'
   ```

   This creates a pre-change identity backup, stops HTTP and identity services,
   switches the identity and pinned owner, then restarts services. If it fails,
   HTTP stays stopped; it does not roll back to old credentials. The old identity
   directory is retained privately as `identity-retired-*`, not served. Memory
   and SSH/stdio are not replaced. Service definitions are checked against the
   selected runtime before any stop command. Readiness waits for the MCP listener,
   not only systemd's `active` state.

   For complete runtime loss, install stopped service definitions pointing to
   the new runtime and restored memory, then add `--lost-runtime`. This mode
   refuses an existing identity or OAuth configuration and does not require a
   backup of missing files. `--expected-owner` is the previous owner from the
   verified quarantine manifest. The new owner comes from the fresh candidate.
   Custom installations can explicitly set `--identity-service`, `--mcp-service`,
   `--provider-port` and `--mcp-port`. Never point a service at quarantine.

5. Use `owner-login` on the active runtime, enroll a **new** passkey and reconnect
   every client. Check the memory workspace ID. Take and verify a fresh OAuth
   backup, and securely remove private retired/quarantine/handoff files when
   recovery has been accepted.

This deliberately logs everyone out after disaster recovery, including the
owner. An old snapshot cannot prove which credentials were revoked afterward.
Changing issuer/domain is not part of this operation: restore requires the exact
expected HTTPS origin. Domain migration needs its own owner-reviewed plan.

## Acceptance test

`deploy/smoke_recovery_installation.py` creates unique temporary Linux user
services, real Pocket ID and MCP processes, and a loopback HTTPS Caddy endpoint.
It writes synthetic messages over HTTPS, backs up memory, a registered original,
a manual wiki file and OAuth, and creates the kit through the installer helper.
It then removes the entire active installation from its expected location,
restores using the offline kit, activates fresh authentication through real
systemd, rejects old access/refresh tokens and cookies, obtains new consent,
reads the same workspace/messages and writes again. Test services and temporary
files are removed even on failure. The test uses local encrypted restic
repositories; S3 backup/restore has a separate pilot check. Physical passkey
enrollment and provisioning a replacement VM are not simulated by this test.

References checked 2026-11-10:
- [Pocket ID account recovery](https://pocket-id.org/docs/troubleshooting/account-recovery)
- [Pocket ID environment and storage](https://pocket-id.org/docs/configuration/environment-variables)
- [Pocket ID export/import](https://pocket-id.org/docs/setup/data-export-import) is
  experimental and destructive on import; this flow does not import old auth state live.
