# Recovery and project folders (0.7)

## Project folders

The assistant performs these steps as part of a requested export. Folder selection is separate
from chat discovery; a binding records the owner's root→memory-project decision. It does not start
a watcher. Existing projects can be created empty with `create_project` before binding.

```sh
python -m portable_context.client project-preferences --profile PROFILE --files include
python -m portable_context.client project-preview --profile PROFILE --root PROJECT_DIR --project-id ID
python -m portable_context.client project-bind --profile PROFILE --root PROJECT_DIR --project-id ID --approve-plan PLAN
python -m portable_context.client project-bindings --profile PROFILE
python -m portable_context.client project-prepare --profile PROFILE --binding BINDING --destination NEW_PACKAGE
python -m portable_context.client send --profile PROFILE --package NEW_PACKAGE
```

Use the same options for preview and bind: `--policy FILE`, repeated `--exclude PATTERN`, optional
`--include-large`. Do not send the source folder wholesale via SCP. The package is screened locally
and contains approved bytes, file passports and an entry page before upload. `send` rechecks local
and server permissions, pins the workspace and resumes batches. Use `--agent` for a sender label.
`project-preferences` without `--files` reads the saved choice; without a saved answer its recommended
value is `include`, but `accepted=false`. That does not authorize new roots.

`discover --format claude-code ...` returns actual session `cwd` and adjacent `memory_roots`.
Include these project memory Markdown folders in the proposed bindings. Never decode directory
names to guess the original path. Different folders may belong to the same project. Reuse bindings
after restart. Keep the private profile, `project-device.json` and `project-bindings/` together;
on another computer create a fresh profile/device binding, not a copied active writer identity.

Defaults exclude dependencies, caches, build output, symlinks, credential filenames and detected
secrets. Files above 50 MiB and model/dataset formats need explicit selection; each file has a hard
512 MiB limit. A scan has a 20,000-observation and 8 MiB manifest limit. Narrow the root when needed.
Large growth (over both 100 MiB and twice the approved bytes) requires reviewing a new preview
before `--accept-growth`. Denied conversations disclose no filenames; denied bytes retain only
allowed observations. A root which is missing or unreadable fails capture; it is never an empty snapshot.

Project files have independent versions and provenance, without fake conversation messages.
A changed hash creates a version; disappearance marks the current file absent while retaining history.
Package sequences prevent late older snapshots and retries from regressing current state. Reappearance
reactivates the file. Roles/purpose/status stay `unknown` unless supplied with evidence; the agent can
annotate passports later. Only the selected root is covered: changes outside it are not inferred.

Useful referenced Write/Edit/NotebookEdit outputs and literal executed-script paths are eligible
in temporary agent directories. Tool-result dumps, task output logs and session JSONL stay excluded.
Current capture policy and source-root restrictions take precedence. Missing temporary bytes cannot
be recreated; export soon after the session. Background delivery is deferred to a later version.

## Self-contained backup

Managed deployment installs pinned, checksum-verified restic. Daily backups contain a consistent
SQLite snapshot, **all archived original bytes**, the vault (including manual Markdown), a manifest
and non-secret memory configuration. SQLite is split into 16 KiB page-aligned files for restic;
restore reconstructs the exact database and checks its hash. Original bytes deduplicate across snapshots
but operational S3 `attachments/` and encrypted restic are two separate copies, intentionally.
Source files never imported into memory and client outboxes are not covered.

The repository is `restic/<workspace_id>/` in the existing private S3 bucket, using existing S3 keys.
Without S3, the installer uses a local repository and explicitly reports it; this protects against
logical mistakes, not loss of the server disk. A reviewed `backup_repository` deployment option can
select another restic repository. Repository migration is not implicit in an update.

Recovery password: `STATE/restic-password` on the deployment computer, and `restic-password` under
the server root. Save a separate copy privately. Losing the password makes encrypted backups
unrecoverable. The installer verifies the same password on update. Secrets are never returned by MCP.

One checkpoint protects each logical import. Resumed batches reuse it. Identical business state
reuses an existing snapshot; skipped days do not occupy retention slots. Backup bookkeeping lives
outside the memory and cannot trigger recursive snapshots. Semantic graph changes, annotations,
policy changes and manual Markdown count as memory changes. Source and vault writes should run
through the service; arbitrary external simultaneous Markdown edits are not transactionally atomic.

Default retention is the union of 14 changed snapshots, 8 occupied calendar weeks, 12 occupied
calendar months and operation checkpoints referenced in the last 90 days. A newly referenced old
snapshot gets a fresh grace period. At least one changed snapshot remains. Restore/check operations
hold a lock and pin their snapshot against pruning. Cron: daily backup 03:17, weekly prune Sunday
03:47, monthly random restore plus `restic check --read-data-subset=5%` on day 1 at 04:17 server time.
The monthly operation downloads a full selected snapshot into a temporary directory, then additionally
checks a sample of file hashes. Space checks reserve room for reconstruction.

MCP: `backup_status`, paginated `list_backups`, `get_backup_policy`, `preview_backup_policy(values)`,
`set_backup_policy(values, expected_version, actor, confirmation)`. A stricter rule does not delete
immediately. Exact bytes reclaimed cannot be predicted per snapshot because restic shares blocks;
report `null` honestly. `storage_report(refresh_s3=true)` inventories current and noncurrent S3 bytes
separately. Pruning restic does not remove provider-retained object versions. No bucket lifecycle is
silently changed. Storage forecasting is an estimate, not a guaranteed size multiplier or a bill.

Local/manual setups can configure `PC_BACKUP_CONFIG` pointing at JSON with `home`, `state` (outside
memory), `binary`, `repository`, `password_file`, optional `s3_config`. Initialize explicitly:
`python -m portable_context.restic_backup --home MEMORY --config CONFIG init`.
Without configuration `backup_status` says not configured; do not describe local imports as protected.

## Upgrade, restore and cleanup

1. Update server using the same private deployment state: `python deploy/setup.py --state STATE install`.
   The old backup runs first; new restic recovery is fully verified before switching the active release
   and cron. During migration MCP reconnects are blocked. On failure rerun the installer; do not manually
   reopen an old writer after a schema change. Then update the client and installed skill; reconnect MCP.
2. Restore a selected full snapshot ID into a new direct child of the server root:
   `python deploy/setup.py --state STATE restore --snapshot ID --target NEW_DIR`.
   For old server-local ZIP: use `--archive ZIP_PATH` instead of `--snapshot`.
3. Preview activation: `python deploy/setup.py --state STATE activate --target NEW_DIR`.
   Explain that imports after the snapshot will need replaying. Close active MCP connections, obtain
   confirmation of the preview, then repeat with `--approve-plan PLAN`. A final checkpoint protects
   the old state, files are reverified and the previous directory is retained. Reconnect clients.
4. `python deploy/setup.py --state STATE cleanup` previews old releases (keeps current/previous),
   completed upload packages and old ZIPs (keeps two per archive directory after restic verification).
   Repeat with `--approve-plan PLAN` only after approval. Active/failed import packages remain.
5. `python deploy/setup.py --state STATE cleanup-s3` separately lists old ZIP **versions** and bytes;
   it keeps the latest two archive names. After explicit approval repeat with `--approve-plan PLAN`.
   It never deletes attachments/restic objects, changes lifecycle or removes the retained old memory.

Owner-only emergency commands run on the server using `python -m portable_context.restic_backup
--home MEMORY --config CONFIG ...`. `bypass-once --run-id ID --actor OWNER --reason REASON
--confirm "IMPORT WITHOUT BACKUP"` authorizes only the named logical run (including its resume).
`disable --actor OWNER --confirm "DISABLE BACKUPS"` and `enable --actor OWNER` explicitly control
protection. These are not MCP tools and must not be used automatically to bypass an error. Decisions
and use are logged outside memory. Scheduled cleanup is also disabled when protection is disabled.
