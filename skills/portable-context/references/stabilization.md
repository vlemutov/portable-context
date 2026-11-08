# Import and recovery in 0.8

Check capability IDs before using new writes: content_identity_v1, resumable_capture_phases_v1,
recovery_coverage_v1, claude_record_taxonomy_v1. Update server/client/skill together and reconnect MCP.

For an existing memory project, `prepare --project-id ID` works without a name. Offline preparation
keeps an unresolved existing-project reference; send validates it against the pinned memory. Supply
both ID and name only for the older user-selected identifier flow, not to invent a display name.

Claude Code defaults to `--diagnostics stub`. API request bodies and prompt snapshots do not become
conversation text or files through nested tool calls. Explicit `--diagnostics full` preserves more
diagnostic evidence but still applies thinking/privacy selection. Inspect per-type omitted-byte counts.
Unknown records remain preserved and reported; do not silently discard them. Rendered roles are
presentation metadata. queued_command/task-notification is an automatic notification; an unknown
mode does not establish a human author. File excerpts and file-history references are not original bytes.

Use `messages` and `project_files` from the run receipt as cumulative operation totals. `pass_messages`
is only the current call. A new replay has a new run ID; resume reuses the saved run ID. Messages are
ingested once per committed batch; transfer work is grouped by unique blob while retaining occurrences.
`timings_seconds` describes measured phases, not a prediction of future upload duration.

Content equivalence is separate from exact archive hashes. `revisions` still counts exact archives;
`content_versions` counts mapped distinct contents and `unmapped_exact_revisions` reports remaining
historical evidence. Metadata-only observations can add exact evidence without being a content edit.
Wiki citations remain pinned to their original revisions. Unknown substantive differences remain distinct.

Historical repair is an owner-operated migration: create/verify a backup, restore into a separate
directory, run `portable-context --home COPY identity-migration`, review equivalent groups and affected
citations, then apply with `--approve-plan DIGEST` on that copy. Compare genuine changes, exact sources,
original hashes and wiki freshness. Use the same preview/apply workflow for the authorized live migration;
do not infer permission to erase archives or activate a different restored state from migration approval.

Before cleanup, `backup_after_import` creates/reuses a current restic snapshot and fully verifies a
temporary restoration. The CLI equivalent is `python -m portable_context.restic_backup --home MEMORY
--config CONFIG post-import`. A pre-import snapshot or sampled check alone cannot prove package coverage.
Inspect the cleanup preview's removable and kept entries, reasons, bytes and covering snapshot. Apply
only the concrete approved plan. Legacy directories are considered only under managed import roots;
unknown/incomplete data stays. No need to repeatedly ask for already-authorized import selection.

For storage forecasting pass unique blob bytes plus record-part AND manifest bytes. Omitted metadata
size returns incomplete_inputs, not a zero estimate. Category byte counts can overlap; do not sum them
as total unique bytes. The storage report separates cache, staging pages/database/originals and journal.
Owner CLI `... restic_backup --home MEMORY --config CONFIG cache-cleanup --max-age-days 30` uses restic's
age-based cache cleanup between operations. It does not remove repository/staging or promise a strict
size cap; evicted cache may need downloading again.
