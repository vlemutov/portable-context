# Portable Context 0.2 call contract

These are MCP call examples, not commands the user must type. The connected server exposes
the precise schemas. Hosts without skill installation can read `portable-context://guide`.

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
