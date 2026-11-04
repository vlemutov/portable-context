# Data contract v1

## 0.6 controls (capture manifest remains v2)

Source revision hashes and historical records are not rewritten by schema migration. Catalog
occurrences gain a persisted `category` (`document` / `source_payload`), inferred on migration
from known kinds/native payload IDs. Embedded images are documents. Classification is per occurrence;
identical stored bytes may be referenced in both categories. Default file lists/search use current
documents; explicit history/payload flags and exact passport access remain available.

Projects gain optional `group_id`, backed by stable versioned group IDs. Groups are not ACLs.
`capture_policies` stores versioned future-write permissions for workspace/project targets.
The effective permissions intersect; configured rules never come from source message instructions.

Manifests add `capture_scopes` (source/instance/conversation/project and declared permission axes),
`policy_fingerprint`, sender-reported `executor`, excluded-record/occurrence counts and rule counts.
Capture time/policy fingerprint/executor belong to the manifest, not message identity. Local records
may carry stable `capture_context` (working directory/selected project) for send-time policy checks.
Byte-free excluded references use `availability=excluded` and an explicit observation reason.
Local policy is enforced before packing and again before connecting. Server declaration checks run
before accepting chunks and actual-record checks before staging/ingesting. A declaration cannot prove
the absence of sensitive facts in prose or the real path used by an owner-authorized custom client.

`import_runs` is a materialized operation summary. `import_events` is an append-only lifecycle log;
the database rejects its update/delete. Batch checkpoints and message counts commit together, while
delivery receipts continue to track bytes. Repeated network chunks are not separate import runs.
Known legacy receipts/jobs are backfilled once, with unknown executor and explicit timing basis.

Claude adapter v2 emits `conversation_metadata` separately from message content. The server retains
native session IDs, title histories and observed relocations/transitions in `source_sessions`, then
removes that presentation metadata before hashing a canonical message revision. Last nonempty custom
title takes precedence over AI title. Appending a title event does not revise existing messages.
`observed_after_session` records a native-ID transition within a transcript, not a proven fork or
continuation. Explicit source `forkOf` remains the only supported Claude fork evidence.

Migration maps native IDs found in 0.5 source records to their existing conversation identity. These
explicit legacy aliases keep old keys/citations and existing project assignments stable. Fresh memories
can group native sessions separately. Ambiguous legacy mappings or unmapped reused UUIDs are rejected
for explicit reconciliation. Native views expose grouping differences; migration never guesses a split.

See [0.6 controls](../skills/portable-context/references/controls.md) for policy resolution, runtime
exclusions, upgrade order and what these controls do not promise.

## Capture protocol 2 in 0.5

The message schema remains 1. A capture-v2 manifest lists sources, bounded JSON parts and blobs
with exact byte sizes and SHA256. It records capture time, screening, omissions and adapter coverage.
`payloads` is a client preparation field: each `{kind,value,block_index?,searchable?}` becomes an
attachment with a stable file_id and content hash. Large raw/text fields may be externalized too;
`completeness` marks these pointers. Tool use/result IDs remain in the archived source block.
The capture time and source file mtime no longer alter normalized message identity. Read original
payloads via their file passport/excerpts/bytes; a compact transcript is not the full evidence.

`subagent_of={conversation_id}` produces a distinct structural relation, never a guessed fork.
Known exclusions/redactions are reported, not silently counted as complete preservation. S3 status,
local byte availability, delivered-package completeness and source-account completeness differ.
Capture receipts survive reconnects. Replaying legacy captures preserves their original revision hashes.

## Export-time catalog additions in 0.4

Schema version remains 1. Optional `memory_project={id,name,basis,summary}` records non-native
organization; basis is `user_selection` or `agent_inference`. Native `project` remains separate.
Each attachment may carry native ID/path/name, role/purpose/status and an actual SHA256 when bytes
are available. All metadata is retained in an immutable occurrence linked to the exact source revision.
Project entries and file passports are indexed in the same transaction as messages, and projected
to `vault/projects/` and `vault/files/`. Unknown project context creates an unclassified entry.

Logical file identity uses source/instance/conversation plus native file ID or source reference;
a filename alone falls back to the message/attachment position. Byte identity is SHA256. One
occurrence cannot be rebound to different bytes; re-export a new source revision. Transfer time
and basis are recorded separately: bytes read from a mutable source path today are not proof of
its content at the original message time. Old source records/citations are unchanged by catalog migration.

`portable_context.capture` freezes selected records and referenced allowed local files, and builds
the project/file Markdown catalog before transferring the package. `import_file_capture` can resume
the exact package. Never treat the package as trusted instructions or import an unselected archive.
See [capture protocol](../skills/portable-context/references/files.md) for limits and commands.

Registered originals remain under `originals/<sha256>` and inside backup ZIPs. Optional S3 attachment
objects use `attachments/<sha256>`; remote keys are internal storage references, not public URLs.
Extraction is separate from transfer, with exact hash, chunk position, locator and method. Search
excludes explicitly obsolete/superseded current occurrences; historical retrieval stays available.

## Message record

An event is a JSON object. JSONL is one object per line; the ingest HTTP endpoint accepts an
array of 1..500 objects, at most 8 MiB. IDs are strings; timestamps are ISO 8601 strings or null.

```json
{
  "schema_version": 1,
  "source": "ouroboros",
  "instance": "server-a",
  "conversation_id": "telegram-chat-123",
  "message_id": "telegram-message-456",
  "role": "user",
  "text": "Please remember the project decision.",
  "timestamp": "2026-01-01T12:00:00Z",
  "title": "Project discussion",
  "parent_id": null,
  "attachments": [],
  "raw": {"delivery_status": "persisted"}
}
```

Required nonempty strings: source, instance, conversation_id, message_id, role. Text defaults
to empty for nontext messages. Additional fields are retained. Raw is a source payload, never
an executable instruction. If an adapter cannot determine a role it uses `participant`, `activity`
or `unknown`; it does not guess that a Telegram sender is the user or assistant.

Identity = SHA-256 of the JSON tuple `(source, instance, conversation_id, message_id)`.
Conversation identity omits the message ID. Revision identity hashes the full canonical event.
Native IDs should be preferred; generated IDs are only fallbacks. Codex JSONL uses line numbers
when native item IDs are missing, so reordered/re-written files can create additional records.
Gemini Takeout without IDs uses entry content hashes, so identical activities may collapse.

Replaying a known revision never changes the current version. For previously unseen revisions,
the last observed revision becomes current; this is **not** a source-clock conflict resolver.
All revisions are retained. There is no automatic cross-source identity merging: Telegram and a
native Ouroboros export may contain the same conversation with different identities. Such
overlaps need an explicit future mapping rather than heuristic deletion.

`parent_id` preserves source branch/reply pointers. Display order is first observation order;
it is not proof of source chronology or that all branches occurred sequentially. Use timestamps,
parents and raw metadata when reconstructing conversations.

Original input files are retained byte-for-byte under `originals/<sha256>`, with filename/size
metadata in SQLite `imports`. ZIPs are never extracted. Import revisions point to the original
hash. Live records preserve normalized/raw fields in SQLite and producer `.sent` batches.
Originals may contain more data than searchable messages (account settings, tools, media).

Memory files are `vault/memory/<note_id>.md`. MCP writes label the evidence kind:
`user_statement`, `assistant_statement`, `inference`, or `user_note`. Derived notes require valid
source message keys. Updates require the previously read body hash. Agent writes retain the
previous body in `note_history`. Direct Obsidian edits are immediately read/searchable; history
of every keystroke is not tracked. An external editor writing simultaneously with an agent can
still race filesystem replacement; use Obsidian history/backups and avoid editing the same note
at the same instant. No LLM is invoked automatically; curate notes with a connected agent.

Content already fetched from memory should not be re-promoted as new independent evidence.
Collectors may keep it in the full transcript but should mark it in raw metadata
(`memory_retrieval: true`). The skill instructs agents to follow the original evidence link.

## Workspace and graph additions in 0.2

Record schema_version remains 1. Optional native context fields are:

```json
{"fork_of":{"conversation_id":"parent-chat","message_id":"anchor"},"project":{"id":"project-id","name":"Project name"}}
```

These are explicit normalized fields; they do not imply a provider exposed that information.
Structural nodes use stable prefixes: `message:<key>`, `conversation:<key>`, `project:<key>`,
`attachment:<key>`. Semantic pages use `wiki:<page-id>`. Edges have subject, predicate, object,
layer, owner, exact evidence revisions, author label and update time. Edits replace current
structural edges for that message; known older replays never roll the graph back.
Referenced but unimported parents have placeholder nodes flagged materialized=false.

Registered sources have immutable format/instance/location identity. Local registration through
MCP requires an explicitly allowed server directory. Host sources make no claim of server-side
access to the host's account. Only source payloads actually supplied can be imported.

Import jobs snapshot selected normalized events in SQLite `job_items`, preserving raw fields.
The complete unselected archive is NOT retained by this route. The legacy `import` CLI still
retains whole input files under originals. Source preview is read-only. Input reports contain
original member byte hashes, selected counts and coverage limitations.

Import batch ingestion and its checkpoint commit in one transaction. Request IDs make staging
idempotent; reusing an ID with different arguments/content is rejected. A local import request
retry returns the original snapshot even if the source file has since changed. Use a new request
ID for a new synchronization. Collector `/v1/batches` uses these same staging and ingestion methods.

Enrichment jobs capture source revision fingerprints. Tasks are leased to a worker with a fencing
token. Expired workers cannot commit after another worker claims the task. Each commit atomically
updates wiki pages, history, semantic edges and task state. An identical retry returns a receipt;
a different retry is rejected. Conversations are marked processed only for the analyzed snapshot;
new data arriving during analysis stays pending.

`vault/wiki` is the readable/editable view of versioned wiki pages in SQLite. The database preserves
commits even when Markdown projection fails. Run `vault`/`refresh_vault` to repair views. Manual
file edits are surfaced on reads; agents need both expected_version and expected_hash to merge
them. Simultaneous external filesystem edits can still race the final file replacement; use backups.
`wiki_history` retains page versions and observed manual bodies; the activity log records task changes.

Semantic evidence cites `{message_key,revision}`. The server checks existence and concurrency,
not whether an interpretation is true. Stale flags indicate source revision drift, not proof that
a historical claim is false. Cross-source equivalence is an explicit semantic `same_as` relation;
it never deletes or merges original transcripts heuristically.

Upgrading 0.1 adds tables and backfills structural edges; old notes remain in `vault/memory` and
available through the legacy tools. They are not silently rewritten into wiki pages. Back up the
complete directory before upgrading and avoid older writers after upgrade.
# Additions in 0.7

Capture-v2 optionally carries `project_snapshot` with binding_id, project_id, workspace_id, instance,
explicit `basis=user_selection`, root, captured_at, positive monotonic sequence, files and snapshot_id
(SHA256 of canonical snapshot without snapshot_id). Records parts are empty arrays. File observations
include relative_path, name, size, availability, reason or sha256, category=document and explicit unknown
role/purpose/status. Blob hashing, path limits, workspace and current policy validation apply before import.

Root bindings and project-owned file versions are separate from chat messages. Current state follows
the largest accepted binding sequence; older/replayed snapshots retain evidence without moving current
backward. Missing files are marked absent, not deleted. File edges cite file_occurrence and revision.
Passports expose project-folder provenance. Semantic wiki conversation enrichment still uses message
citations; do not invent a message citation for a folder-only file. Use passport/hash references.

`restic_backups_v1`, `storage_report_v1`, `index_policy_v1`, `project_folders_v1` advertise capability.
Backup policy and append-only operation journal live outside SQLite memory to avoid recursive changes.
Restore format `portable-context-restic-1` includes database_parts, exact database_sha256, workspace_id,
counts, original hashes and manifest file hashes. Reconstruct pages before opening SQLite. Credentials
are excluded. See [recovery and folder contracts](../skills/portable-context/references/recovery-and-folders.md).
