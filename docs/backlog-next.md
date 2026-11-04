# Backlog after the 0.7 pilot

Status: release split approved, 2026-11-04; 0.7.1 implementation authorized.
0.8 migrations and historical cleanup still require their separate review. Requirement numbers below
refer to the owner's sixteen-item pilot report. Personal transcripts and logs stay outside Git.

## Findings verified against 0.7

- R1 reproduced with a synthetic single native session: `display_sessions` passes `native_id`
  twice to `dict`, raising TypeError. Mixed legacy grouping is not necessary to trigger it.
- R2 reproduced: adding capture_context alone produces revised=1. Revision identity currently
  hashes the complete normalized record, including export-time metadata. Prior tests covered
  identical-package replay; they did not prove stability across new exports/client representations.
- R3: the adapter retains the user envelope for compact summaries; generation provenance is not
  propagated consistently into recall/enrichment. Flags need explicit handling and fixtures.
- R6: every finish call re-enters import_capture, reloads records, resolves sessions and traverses
  all message batches, although only the next 25 attachment occurrences are transferred.
  Exact time spent in SQLite versus S3 remains to be measured.
- R7: final replies expose the last pass's message/file counts. `initial_messages` is package-level
  receipt state, not a sound replacement for run-scoped cumulative accounting.
- R8/R9: prepare requires both project ID and name; cwd is seeded from the first record and not
  accumulated. Memory roots are suggested by adjacency without detecting multiple real workspaces.
- R10–R14: cleanup lacks retained-item explanations and exact snapshot coverage, scans only
  imports/uploads, storage groups restic cache/staging together, and forecast treats omitted
  session-record bytes as zero. Reported private sizes/timings are pilot observations, not measured here.
- R15/R16: enrichment coverage is chunk/conversation based, without a persisted partial-review model.

## Release sequence

| Release | Scope | Exit condition |
|---|---|---|
| 0.7.1 hotfix | R1, R3, R9a/b | Recall works; generated summaries cannot masquerade as user statements; ambiguous folder ownership is explicit. No revision-hash migration. |
| 0.8 stabilization | R2, R4–R8, R10–R14 | New exports preserve content identity; resumable import reports one operation accurately; storage/cleanup decisions have verifiable evidence. |
| 0.9 enrichment | R15, R16 together | Byte/token-aware evidence batches and honest full/selective/unseen coverage share one model. |
| After stabilization | Multi-client onboarding, marketplace packaging, background delivery | Stable identity, attribution, receipts and checkpoint coverage first. Existing manual workflow remains supported. |

The content-identity repair is the first work item in 0.8, before performance changes or another
large enrichment run. Do not defer it behind convenience features. If its scope proves small enough
to ship sooner, make a separate stabilization release; do not hide a cross-adapter migration in a hotfix.

## 0.7.1: unblock reading and protect attribution

### R1 — conversation listing [server]

- Assemble native-session display metadata with one authoritative native_id; preserve aliases and
  legacy conversation grouping. Do not split or merge existing conversations to fix a display error.
- Isolate recoverable per-row metadata errors: return that conversation's stable ID and a bounded
  diagnostic with `partial=true`. Preserve page positions/next cursor, never silently skip rows.
  Database-wide failures still fail explicitly; do not turn arbitrary exceptions into empty success.
- Check conversation Markdown projection as well as list_conversations: both call display_sessions.
- Acceptance: one/multiple native sessions, legacy aliases, missing/duplicate metadata, malformed
  per-row details, pagination containing the bad row, and unchanged historical message identities.

### R3 — compact-summary provenance [client, server, skill]

- Keep the original envelope/flags as source evidence. Expose a distinct generated-summary kind
  and effective author attribution in search, reads, enrichment claims and Markdown.
- `isCompactSummary` is evidence for this classification; `isVisibleInTranscriptOnly` alone must
  not classify unrelated records as summaries. Summary text is model-generated context, not a user quote.
- Repair the effective classification of known stored records additively, without overwriting their
  archived source versions. Surface wiki claims that may have mistaken the summary for user intent.
- Acceptance: generated summaries, ordinary user messages and unrelated visibility-only records.

### R9 — workspace discovery and ambiguous memory folders [client, skill]

- Aggregate nonempty cwd and relocatedCwd observations across all records, with distinct native
  message counts, observation type/time and local path-existence status. Avoid counting relocated
  targets as proof that messages were actually written there. Do not equate unreadable with absent.
- Deduplicate copies by native identity, not encoded directory names or file counts.
- Detect multiple observed real workspace roots sharing one encoded source directory. Mark adjacent
  memory roots ambiguous; display candidates/evidence and require an explicit ownership choice.
  A relocation can explain multiple roots but is not proof that a shared memory folder belongs to both.
- Do not automatically alter previously accepted bindings; offer a separate audit of suspicious ones.
- Acceptance: first row lacks cwd; later rows provide it; relocated sessions; duplicate files;
  Cyrillic-name collisions; missing/unreadable roots; Windows/macOS/Linux paths.

## 0.8: trustworthy identity and efficient ingestion

### R2 — content identity and historical equivalence [server, all clients, contract, skill]

- Separate immutable source content, export observations and memory organization. Introduce a
  versioned content-hash contract; retain original record hashes and exact archived revisions.
- Export context, selected memory project, client/build time and capture policy are observations,
  not content-version inputs. Retain their histories and actor/basis instead of overwriting silently.
- Content includes author/role, text, native reply/fork relations, source timestamps, native attachment
  references and substantive tool/media payloads. Define relevant raw fields per adapter rather than
  dropping all raw metadata or comparing only visible text. Unknown changes stay conservative.
- Different chunking, payload externalization or a changed exporter must not change semantic identity
  when the preserved content is provably equivalent. A partially redacted/omitted export must not
  falsely establish equivalence to a fuller one. Captured current file bytes are separate file evidence.
- For existing metadata-only revisions, add an auditable equivalence mapping. Keep original wiki
  citations pinned to their exact versions; use proven equivalence to assess freshness. Do not
  silently rewrite citation history or clear stale flags caused by real content/attribution changes.
- Migration first produces a read-only plan: equivalent revisions, uncertain cases and affected pages/
  edges. Test backup/restore and apply on a copy. Keep historical revisions and originals; deletion
  or physical compaction is a separate operation, not part of semantic repair.
- Acceptance: rebuild by another client, project selected in prepare versus send, cwd/client/policy
  metadata changes, different payload representation, genuine text/role/tool-result changes,
  partial exports, old-package replay after a later real edit, existing citations and restored migration.

### R4/R5 — source record classification [client, contract, skill]

- Use an explicit adapter taxonomy: conversation, tool call/result, generated summary, attachment
  context, structural event and diagnostics. Unknown types remain visible with a coverage warning.
- Default known diagnostic types to a stub with type/time/stable source identity. Include their full
  payload only through an explicit local option. Prevent duplication through raw fields or another
  payload route. Prepare reports counts and omitted bytes by type, not just a total warning count.
- Preserve useful structural metadata: titles, relocation/fork evidence and parent links are not
  disposable merely because their records are non-user messages. Local source transcripts remain untouched.
- Parse attachment subtypes individually. File references, inline context and hook output are different
  evidence; do not fabricate a file or follow arbitrary paths just because the outer type is attachment.
- Require explicit kind for new payloads. Repair old missing kinds only from reliable source structure;
  otherwise label unknown with a reason, without revising the original message as a side effect.
- Acceptance: fixtures for each reported diagnostic family, attachment subtype, compact summary and
  missing-kind case; default contains no full request blob through another field; explicit opt-in works.

### R6/R7 — resumable importer and operation receipts [client, server]

- Persist phases: validate package, ingest messages once, transfer unique blobs, attach occurrences,
  build projections, finish receipt. Checkpoint once per run; resume committed work instead of replaying
  all message rows on each attachment batch. Continue enforcing policy when resuming.
- Deduplicate transfer/verification by content hash while retaining occurrence provenance. Batch SQLite
  writes and reuse connections. Tune bounded batches by bytes/time/object count; keep MCP replies small.
  Do not trade away checksums, fsync guarantees or S3 verification merely for speed.
- Add timing counters for parsing, session resolution, SQLite, hashing, S3 operations and projection.
  Progress shows phase, records, occurrences, unique blobs and bytes. ETA is unknown until measured,
  then explicitly estimated; record progress must not imply attachments are already stored.
- One authoritative receipt per run_id for messages and project files; separate cumulative operation
  counts from this pass's work. A fresh replay is a new run with unchanged counts, not a reused first
  import total. Failed/resumed runs keep their accumulated results exactly once.
- Acceptance: interrupted/repeated delivery, duplicate hashes across occurrences, current policy
  changes, new replay of an old package, late folder snapshots, accurate end-to-end totals.
  Benchmark approximately 6,000 records / 4,500 small occurrences; metadata ingestion must not scale
  with the number of transfer rounds. Set a latency target after measuring the actual bottleneck.

### R8 — existing-project selection [client, server]

- Existing project ID suffices. With a profile, resolve its display name against the pinned workspace.
  Offline prepare keeps an unresolved organization reference for send to validate; do not require a
  fictitious title, create another project or silently connect to a different memory.
- Native source project metadata remains distinct. This uses the R2 observation/organization model.

### R10–R12 — explainable cleanup and exact recovery coverage [server, deploy]

- Preview both remove and keep, with path, bytes, timestamp source, reason and retention rule.
- Report whether a completed package's messages/file versions/original hashes are covered by a
  specific verified restic snapshot. A pre-import checkpoint does not cover the subsequent import;
  timestamps alone are not sufficient proof. Add a verifiable coverage receipt/manifest.
- Default to keeping packages with unverified coverage and offer a post-import backup, then recompute
  the preview. A separately acknowledged exception must explain what recovery would lose.
- Discover legacy packages only inside explicitly managed import roots. Identify format/manifests,
  verify their records/blobs are represented in memory, and protect incomplete/unknown directories.
  Do not recursively treat arbitrary server directories as disposable packages.
- Acceptance: import after newest snapshot, partial import, replay of existing content, older package
  formats, unmatched blobs, concurrent upload and stale approval. Deletion stays preview/confirmation based.

### R13/R14 — cache accounting and realistic forecast [server, client, deploy]

- Separate restic cache, staging SQLite/page chunks/originals, journal, local repository and archives.
  Distinguish logical sizes from allocated disk usage/hard links where supported.
- Cache is rebuildable; journal, repository and recovery credentials are not cache. Cleanup should
  run only between restic operations, never while backup/restore/prune is active. Expose a supported
  age/size policy and report evicted bytes versus expected re-download cost; avoid unsupported promises
  of a strict bound on an actively used repository cache.
- Prepare emits explicit record-part bytes, unique blob bytes by category, occurrence counts and
  package bytes. Forecast consumes these directly and defines its units to prevent double counting.
  For session packages, missing record bytes means unknown/incomplete estimate, not zero.
  Folder-only packages still include passport/index overhead. Show expected growth and temporary peak.
- Acceptance: session-only, folder-only, mixed package, known existing blobs, compressed/expanded
  source formats, missing size information and backup staging included in disk reserve.

## 0.9: enrichment without false completeness (R15/R16)

- Build on stable content identity and correct source attribution. Preview total evidence volume,
  estimated context/token budget and coverage before starting; estimates depend on the chosen model.
- Group tool calls with their results; bound by bytes/tokens rather than a fixed message count alone.
  For an oversized pair, return an explicit manifest and on-demand excerpts, not a silent split or
  an unbounded prompt. Tool outputs are a distinct cited evidence type.
- Persist a review ledger for exact content revisions: fully reviewed, selectively reviewed, unseen.
  A scaffold page records the subset inspected, its evidence and limitations. Never mark its whole
  conversation or leased chunk complete because one page was written.
- Keep page publication, review coverage and task completion distinct. Support incremental follow-up,
  partial progress per project, correction of stale evidence and concurrent workers without lost updates.
- Acceptance: selective reading, tool-only evidence, oversized tool pair, new messages arriving during
  analysis, equivalence-only revision changes, real content changes and cross-agent resume.

## Minimal additional evidence requested from the pilot

1. Versions: local package/skill and server; commands/options for discover/prepare/send; package ID,
   run ID and paths to locally retained artifacts. Native IDs are useful; no credentials or full archive needed.
2. Adapter fixtures: sanitized original JSONL records for compact summary, a visibility-only example,
   each observed attachment subtype and 2–3 missing-kind passports with their originating records.
   Preserve field names, types, booleans and ID relationships while replacing private text.
3. Identity: old/new normalized records for the same message (including attachment metadata and hashes),
   plus one wiki citation marked stale. One example for capture_context-only and one for project selection.
4. Performance/accounting: one slow run's final client JSON, server run summary, available per-batch
   timestamps, unique blob counts/bytes and prepare report; project-folder run's mismatched receipt too.
   Do not rerun a large import just to collect logs that do not yet exist; add instrumentation first.
5. Discovery/legacy cleanup: sanitized cwd/relocatedCwd observations for the shared encoded directory;
   legacy package directory listing and manifest headers only, without document contents.

R1 does not need another production traceback to start: its cause is reproduced. R2's mechanism is
also confirmed; paired records are needed to validate migration coverage, not to establish that it is a bug.
Implementation can start with synthetic regressions while the missing adapter/measurement evidence is gathered.
