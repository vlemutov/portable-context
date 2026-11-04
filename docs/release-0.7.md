# Portable Context 0.7 — release and acceptance

Agreed scope: self-contained incremental restic backups **including original bytes**, bounded
retention, verified restore and rollback, storage accounting, lighter service-event indexing,
useful temporary files, and explicit project-folder bindings/import. New work must preserve 0.6
source records, revisions, assignments and original hashes.

1. Measure actual restic growth on a private restored copy and a synthetic corpus. Keep ZIP recovery.
2. Back up consistent SQLite, original bytes, manual Markdown and non-secret memory configuration.
   Persist the restic password privately on server and deployment client; never print credentials.
3. One checkpoint before the first mutation of each logical import/migration. Resume shares it.
   Reuse an existing checkpoint for identical content. Failed protection blocks managed imports.
4. Retain 14 changed snapshots, 8 weekly, 12 monthly; operation checkpoints for 90 days. Preview,
   optimistic versioning and explicit confirmation for reductions. Restore/check pins prevent deletion.
5. Restore into a new directory and verify before explicit activation. Monthly restore/check;
   daily backup; weekly approved retention. No destructive migration cleanup without preview.
6. Measure memory, staging, local archives and S3 (including old object versions separately).
   Forecast peak space with documented uncertainty; do not promise a universal size multiplier.
7. Project files enabled as the recommended onboarding choice; a concrete root-to-project binding
   is explicitly accepted once. Capture documents/source/results without fabricated chat messages.
   Preserve file versions and absence observations, exclude dependencies/cache/secrets by default.
8. Useful referenced temporary outputs and scripts are eligible; tool-results/tasks/session duplicates
   remain excluded. Capture-policy and authorized filesystem roots always take precedence.
9. Service-event indexing is configurable and off by default; originals remain recoverable.

0.8: enrichment budgets/chunking/evidence, multi-client connection setup, Claude marketplace packaging,
then opt-in background capture and delivery. A folder binding does not authorize background recording.

Acceptance: synthetic Windows/macOS/Linux client tests, Linux server/restic tests; no private fixtures
in CI. Test no-change, changed import, resumed import, corrupted/missing originals, failed backup,
retention/pins, restore, safe cleanup, folder traversal/symlinks/secrets, historical file versions,
and unchanged source evidence across migration. Record measured growth before publishing 0.7.

## Measurements and practical limits

On a private restored database, 100 synthetic messages (114,391 serialized bytes) added 5,551,334
packed bytes to the 16 KiB page-chunk restic repository; the first snapshot added 62,506,694 bytes.
A consecutive unchanged backup was skipped. The earlier whole-SQLite experiment added 21,094,993
bytes for a similar 100-message import. Page chunks improve this case by about 3.8× but introduce
more small files and a larger initial tree. The goal of a delta as small as the input is **not met
for this small import**: SQLite indexes and page changes amplify it. Do not promise a 1:1 multiplier.
These exploratory measurements use successive states of a restored copy, not a controlled same-state
comparison. All archived originals are included in both designs.

Synthetic CI fixture: 209,491 source bytes produced 940,695 bytes of memory with defaults versus
1,415,831 with service-event indexing enabled (about 34% smaller; 4.49× versus 6.76× input).
This measures the policy difference, not a universal improvement over 0.6: that version already
avoided indexing records explicitly marked non-searchable. Existing extracted chunks and freed
SQLite pages are retained; physical files do not automatically shrink after migration.

Originals deduplicate within restic. Operational S3 originals and encrypted recovery originals are
two separate copies. Provider historical object versions may retain bytes after prune and are counted
separately. No bucket lifecycle is changed silently. Local backup staging holds a consistent database,
page chunks and originals. Folder captures also require local snapshot space.

Folders are captured by the assistant during subsequent requested exports using saved bindings;
no watcher is enabled. Known verified original hashes satisfy new uploads without retransmission.
Out-of-order packages cannot regress current files; unobserved/excluded scope is not deletion.
Folder files have independent passports, not fabricated chat messages. Conversation wiki enrichment
still uses message citations; use file passports/hashes for folder-only evidence.

## Update and first test

1. Run `python deploy/setup.py --state STATE install` from the new checkout using the same private
   deployment state. Old backup runs first; full restic restoration is verified before switching.
   During migration MCP reconnects are gated. If interrupted, rerun the installer; do not reopen an
   old writer after schema changes. Server installation requires Linux with Python 3.11+, SSH and cron.
2. Update the local client and installed skill copies (including uploaded skill ZIPs), then reconnect
   MCP. Existing SSH/stdio connection settings remain valid. Verify workspace ID/version/backup status.
3. Save the recovery password privately outside the server. Confirm the default retention and the
   recommended inclusion of selected project folders conversationally; reuse answers already given.
4. Replay a selected Claude capture. Expect unchanged source messages and reused originals. Bind only
   intended project roots after the size/exclusion preview; inspect passports and historical versions.
5. Review storage and cleanup previews. Deletion of old ZIPs/S3 versions and activation of a restored
   memory each require a concrete separate approval. The previous memory directory is retained.

Exact commands, emergency bypass/disable controls and agent behavior:
[recovery and folders](../skills/portable-context/references/recovery-and-folders.md).

## Validation

Synthetic tests cover real restic, blocked checkpoint/resume, self-contained originals, corruption,
retention/pins, activation tampering, indexing, cleanup, folder versions, reused uploads and privacy.
The CI matrix includes Windows, macOS Intel/ARM and Linux x64/ARM. Check the release's Actions result
before treating every target as verified. Private migration checks run on a separate restored copy;
no personal fixture is published. Unconfigured local stores explicitly report unprotected imports.
Restic passwords and provider credentials never appear in MCP replies. No regular VACUUM is run.
