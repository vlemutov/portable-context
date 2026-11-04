# Portable Context 0.6 call contract

These are MCP call examples, not commands the user must type. The connected server exposes
the precise schemas. Hosts without skill installation can read `portable-context://guide`.

## Controls added in 0.6

Read [controls.md](controls.md) before changing capture permissions or migrating groups.
`memory_status` exposes capability IDs, recommended skill version and three recent import operations.
`list_files`/`search_files` now default to documents; pass `include_payloads=true` for source copies.
`include_history` remains independent of category. `read_file` accepts any exact known passport ID.

- `list_project_groups`, `save_project_group(identifier,title,actor,expected_version=0)`;
  `list_projects(group_id=...)`, `create_project(group_id=...)`, `update_project(group_id=...,clear_group=false)`.
  Omitted group retains membership. `preview_project_group_migration` is read-only.
- `get_capture_policy(project_id?)`, `set_capture_policy(permissions,actor,expected_version,project_id?)`.
  Permissions are boolean `conversation`, `tool_payloads`, `document_bytes`; omitted project is workspace-wide.
- `list_imports(limit,offset)`, `read_import_events(run_id,limit,offset)`; executor labels are self-reported.
  Scope previews are bounded; `read_import_scope(run_id,field,limit,offset)` pages conversations/projects/sources.
- `list_source_sessions(source?,limit,offset)` exposes native IDs, title/relocation history and legacy aliases.

0.6 `begin_capture_upload(manifest,project_id?,executor?,run_id?)` preflights policy and returns both `upload_id`
and `run_id`. Keep that run ID on every `finish_capture_upload(upload_id,project_id?,run_id)` during
the same delivery. Target project is fixed at begin; a changed policy can refuse later chunks.
A separate user-initiated replay starts a separate run; retrying a completed finish is idempotent.
Generate and save a UUID `run_id` before begin for safe retries even if the begin response is lost.
Host `stage_records`, `stage_export` and `start_import` also accept `executor={client,version,agent}`.

## Source and import

```json
{"tool":"connect_source","arguments":{"source_id":"claude-personal","label":"My Claude export","format":"claude","instance":"claude-personal","location":"/authorized/exports/claude.zip"}}
```

`location` refers to the server. Allowed paths are set with `PC_SOURCE_ROOTS` (OS path-list
separator) or repeatable `mcp --source-root`. The local CLI `connect` explicitly registers a path.
Do not ask the user to expose their whole home directory just to import one export.

```json
{"tool":"start_import","arguments":{"source_id":"claude-personal","conversation_keys":["key-returned-by-preview"],"include_forks":true,"request_id":"stable-import-request-id"}}
{"tool":"run_import","arguments":{"job_id":"returned-job-id","batch_size":100}}
```

To bridge host history, omit location on connect_source. Register format `ouroboros`, `hermes`,
`openclaw`, a provider format, or `events` for mixed normalized sources. Each record instance must
match the source registration. Except with `events`, the record source must match its format.

```json
{"tool":"stage_records","arguments":{"source_id":"ouroboros-vps","request_id":"stable-batch-id","records":[{"source":"ouroboros","instance":"vps-a","conversation_id":"chat-1","message_id":"msg-5","role":"user","text":"Use local storage.","timestamp":"2026-01-01T12:00:00Z","title":"Project Atlas","parent_id":"msg-4","project":{"id":"atlas","name":"Atlas"},"raw":{"native_id":"msg-5"}}]}}
```

Optional native conversation fork: `"fork_of":{"conversation_id":"original-chat","message_id":"fork-anchor"}`.
Do not invent it from matching text. `attachments` is an array of preserved source references;
binary content is not fetched by this call. Custom context can be carried in preserved raw fields;
there is no automatic account settings/project-file extractor.

Project/file export is described in [files.md](files.md). `memory_project={id,name,basis,summary}`
records user-selected or inferred grouping separately from native metadata. Import creates passports
and project entries transactionally; `run_import` returns catalog counts and projection conflicts.
`store_attachment` binds verified bytes to an occurrence ID from `list_files`; `import_file_capture`
resumes a selected client snapshot. Record completion alone does not prove attachment delivery.

For host exports use `stage_export(source_id, content, filename, request_id)`: at most 8 MiB of
text and 500 parsed records. It accepts already-selected content; local preview/select is preferred
for large mixed archives. Selection occurs before transmission, not after the server stores it.

## Enrichment

`start_enrichment` returns a job. `claim_enrichment(job_id, worker)` returns task position,
lease token, expiry, source revisions and previews. A task is a chunk of one conversation;
it is not necessarily the entire discussion. Paginate `read_source_revision` and use conversation
tools for context. `renew_work` extends a still-valid lease; if expired, claim again.

Example page inside a commit:

```json
{
  "id":"atlas-storage",
  "title":"Atlas storage decision",
  "text":"The user chose local storage for Atlas. Earlier alternatives remain documented in the source.",
  "kind":"user_statement",
  "sources":[{"message_key":"actual-message-key","revision":"actual-revision-hash"}],
  "expected_version":0,
  "relations":[{
    "type":"about",
    "target":"conversation:actual-conversation-key",
    "reason":"The decision concerns the Atlas discussion."
  }]
}
```

Existing page: obtain `version` and `hash` from `read_wiki`, pass `expected_version` and
`expected_hash`, and supply the merged page text. Returned `body` includes generated metadata,
sources and related links; update the prose in `text`, not a verbatim nested copy of that wrapper.

`commit_enrichment(job_id, position, lease_token, pages, summary, no_change_reason?)` atomically
saves the pages, their history, semantic links and task completion. New page version is 0;
omit expected_hash. All pages require exact existing revision citations. Relations default to
page sources; provide their own sources if different. Types: `related_to`, `about`, `supports`,
`contradicts`, `supersedes`, `same_as`. Targets must exist or be pages created in the same commit.

Use `pages:[]` with a meaningful no_change_reason if evidence warrants no durable update.
On a lost response, retry the exact commit with the same token. A changed retry is rejected.
The receipt proves storage, not factual correctness. Source changes mark citations stale;
new messages arriving during analysis remain eligible for the next enrichment run.

## Useful status tools

- `memory_status`: workspace identity, connected sources, pending conversations and jobs.
- `get_job` / `list_jobs`: checkpoints and results; import and enrichment completion are distinct.
- `inspect_graph`: graph node IDs, layer, typed relations, evidence and stale flags; paginate.
- `search_wiki` / `read_wiki`: current body, manual edits, versions and hashes.
- `check_memory`: deterministic consistency checks; not a model-based contradiction detector.
- `refresh_vault`: repair Markdown projections; report `.incoming.md` conflicts without deleting edits.
- `list_projects` / `read_project` / `update_project`: project entry and versioned agent assignment.
- `list_files` / `read_file`: current or historical file passports and transfer/extraction states.
- `search_files` / `read_file_excerpts`: scoped text retrieval with exact file hash and locator.
- `store_attachment` / `read_file_bytes`: bounded, verified original transfer; no automatic URL fetches.
- `add_file_excerpts`: externally extracted text with method/actor attribution and locators.
- `import_file_capture`: selected server-local package made by `python -m portable_context.capture`.
# MCP additions in 0.7

- `backup_status()` is cached/readonly; `list_backups(limit=20, offset=0)` returns paginated snapshots,
  reasons, operation links and added bytes. Restore/activation stay in the owner deployment CLI.
- `get_backup_policy()`, `preview_backup_policy(values)`, `set_backup_policy(values, expected_version,
  actor, confirmation=null)` use last/weekly/monthly/checkpoints_days. Removing existing snapshots
  requires the current exact preview digest; actual deletion occurs at scheduled cleanup.
- `storage_report(refresh_s3=false)` inventories SQLite tables, originals, vault, staging, releases,
  archives, free disk and cached S3 versions. `forecast_import(original_bytes, source_bytes=0)` estimates
  additional memory and peak disk with uncertainty; it is not a price quote.
- `get_index_policy()`, `set_index_policy(kind, enabled, expected_version, actor)` control rebuildable
  search indexes by payload kind without discarding originals or running VACUUM.
- Project-folder packages use normal begin/upload/finish capture tools and require `project_folders_v1`.
  Their receipt has zero message counts plus independent project_files counts and usual blob/S3 receipts.
  See [recovery-and-folders.md](recovery-and-folders.md) for agent decisions and local commands.
