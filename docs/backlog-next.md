# Backlog after the 0.7 pilot

Status: 0.9 shipped and the two-conversation end-to-end pilot verified, 2026-11-09. At the owner's request,
return to data/import stabilization as the next release; the access/authentication proposal is deferred.
0.8 and 0.9 are shipped. Destructive cleanup still requires its concrete preview.
R-numbers refer to the sixteen-item pilot. Raw diagnostics and personal source examples remain private.

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
| 0.7.1 hotfix — shipped | R1, R3, R9a/b | Recall, summary attribution and workspace ambiguity fixes are already delivered; new diagnostic summary fixtures also pass current classification. |
| 0.8 stabilization — shipped | R2, R4–R8, R10–R14 | Re-exports preserve content identity; imports resume without repeated ingestion; operation totals and verified recovery coverage are trustworthy. |
| 0.9 memory/wiki stabilization — shipped | R15/R16 minimum plus proposed 0.8.1 fixes | Mixed-message authorship, selective review/resume, repeat-delivery performance and full S3 restore verified. 166 tests; all 13 platform/Python and 6 deployment CI jobs passed. |
| Deferred, no release assigned | HTTPS/device credentials/OAuth, local installation tiers, domain onboarding | Keep [the access proposal](access-and-installation-plan.md) for later review; do not change the live access path as part of stabilization. |
| Later | Marketplace packaging, background delivery, offline multi-writer sync, optional tunnels | Separate decisions after the core workflow is reliable. |

The intermediate proposal to make 0.8 an access release is superseded. Original data work returns to
0.8 and starts with content identity. Do not mix a transport/authorization rollout into this migration.
The diagnostic packet is sufficient to start the core work; the requested service-type and role/mode
fixtures have arrived. Optional linked examples would improve edge-case coverage, not block implementation.

## Diagnostic review: corrections and implementation order

### Evidence confirmed locally

- The supplied user/tool pairs are equivalent after removing capture_context and memory_project and
  deriving attachment category consistently. The tool payload SHA256 is unchanged. The user text in
  the packet is truncated, so these are diagnostic examples, not checksum-valid immutable originals.
- Historical change summaries also contain text, parent_id, role and raw changes. Do not classify all
  old revisions as export noise. Validate exact original evidence on a restored copy for migration.
- Current code reparses packages and re-enters all message ingestion batches on every transfer round.
  The batch ledger suppresses duplicate counting but does not suppress that work. Missing phase timings
  prevent attributing the elapsed time precisely to SQLite, S3, parsing or backup.
- Folder sends expose final-pass unchanged counts while the server's run ledger records inserted files.
  One run-scoped cumulative receipt must serve both client and server; fresh replay remains a new run.
- All supplied attachment subtypes currently become generic service_event payloads with empty visible
  text and no document passports. Raw evidence survives, but file/context semantics are not represented.
- Missing payload kind is NOT established for the supplied document passports. category=document and
  reference_kind=tool_file are valid without kind. Their unavailable reason is outside_allowed_roots;
  do not widen allowed roots or invent a kind to repair that. Audit the full reported set before generalizing.
- Compact-summary samples are correctly recognized by 0.7.1. Encoded-folder collisions reinforce its
  conservative behavior; nested cwd and relocation paths are not proof of separate projects or ownership.
- Legacy package manifests identify format-v1 directories outside uploads. Age and format alone cannot
  establish that their records/blobs have been imported and are covered by a verified backup.

### 0.8 implementation sequence

1. R2 + R8: versioned content identity, capture/organization observations, safe equivalence mapping,
   unchanged exact citations, project-by-ID selection, and migration preview on a restored copy.
2. R6 + R7: persist import phases; ingest once per run; transfer/check unique blobs once while retaining
   occurrences; return authoritative cumulative receipts. Add phase/batch monotonic timings and counters
   before tuning batch sizes. Test interruption, replay, resumed-policy enforcement and project folders.
3. R4 + R5: classify actual attachment subtypes, not just the outer attachment record. Distinguish file
   bytes versus excerpts/references, context/reminders, task notifications and structural/completeness evidence
   from bulky diagnostics. Stub only known diagnostic data by default, with an explicit full option;
   prevent its reappearance through raw/rendered/service_event duplication. Retain unknown evidence.
4. R10–R14: verified post-import backup coverage, explainable remove/keep previews, managed-root legacy
   package recognition, cache accounting and accurate source/unique-blob/peak-space forecast.
5. Pilot: replay the identical package, rebuild the same selected source slice, then add a genuine change.
   Verify source versions, wiki freshness, totals, original hashes and recovery on each case. Do not
   benchmark by repeatedly importing the real full archive before instrumentation exists.

### Additional adapter evidence reviewed on November 8

- All eight requested service families are represented by 23 examples; a further 25 attachment examples
  cover 20 subtype/renderedRole/commandMode combinations. Isolated local conversion on 0.7.1 produces
  unknown-type warnings and service_event payloads for all 48, with no extracted document references.
  Three file-history-delta examples require a synthetic session anchor when tested in isolation.
- api-request-blob also produces nonempty visible text from its nested message. Classification must
  precede generic content/tool parsing; a diagnostic stub must not leak full content through text,
  tool-derived attachments, raw, rendered or service_event. Apply privacy/thinking policy to full mode too.
- renderedRole is presentation metadata, not human authorship: session_context, instructions and
  remote_session_change can render as user. All supplied queued_command examples use task-notification;
  retain notification provenance and known origin/delivery references, without inventing a user command.
  Missing/unknown commandMode must not default to human input. Keep source envelopes unchanged.
- file-history-delta carries messageId/snapshotMessageId, path and backup metadata. Preserve these as
  structural evidence; they do not establish conversation forks or the existence of original bytes.
  Supplied backupFileName values are null. A tracking path is not permission to read outside allowed roots.
- Keep small mode/tools-used/cost-state observations as typed session metadata, outside conversational
  claims. Treat cumulative cost snapshots as snapshots, not additive charges. atis-latch examples only
  contain an empty value: preserve a conservative stub and flag unsupported nonempty shapes.
- Preserve native request IDs and shape/blob/base references as typed diagnostic references, not reply
  or fork edges. Do not invent timestamps where absent. Policy-dependent stubs must not become the
  source of event identity; repeated identical state observations need an explicit occurrence policy.
- File snippets and reread metadata are not original bytes. Even a complete line range establishes
  captured text coverage, not byte-for-byte identity with a historical file.
- Sanitization replaces hashes/paths with length markers and parses embedded JSON strings. These
  examples establish shapes, not checksum validity, exact original wire types or reference equality.
  Use synthetic linked fixtures for string/object forms, distinct hashes, repeated states and partial files.

Optional follow-up only: one small consistently pseudonymized linked request/blob/shape sequence;
a file-history delta with non-null backupFileName if present; queued_command modes other than
task-notification if actually observed. Preserve origin.kind/producer and reference equality in those
examples. No full transcript, unredacted text or another large import is required.

## 0.7.1: shipped — unblock reading and protect attribution

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
- Distinguish rendered role from authorship and task notifications from human input. Classify service
  records before parsing nested message content. Preserve typed structural/file-history references and
  compact telemetry without exposing them as user claims; see the November 8 evidence above.
- Require explicit kind for new source payloads; do not require payload kind for document passports.
  Repair genuinely missing payload kinds only from reliable source structure;
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

## 0.8.1 proposal incorporated into 0.9: repeat delivery and backup workspace

Verified after the 0.8 pilot; implementation and validation are included in 0.9. No authentication/transport change.

- **P1 — S3 verification reuse.** `sync_blob` currently ignores its persisted verification time
  and performs HEAD plus full GET on every delivery. Persist proof scoped to the storage target,
  object key/version, hash and size. A recent prior proof may be reused with an explicit
  `previously_verified`/reused status and its original timestamp, never reported as a new remote
  integrity check. Define expiry and invalidation for target changes, failed verification and
  explicit audits. First upload and untrusted/changed objects still require byte verification;
  forced/periodic audit must detect deletion/corruption. Policy checks remain on every import.
  Acceptance: unchanged delivery avoids full S3 GETs within the defined reuse window; test target
  switch, expiry, prior failure, explicit audit, missing/corrupt object and interrupted upload.
- **P1 — unchanged backup overhead and timing.** `validate` includes protection/checkpoint
  staging. Report checkpoint, manifest/policy validation, transfer wall time and summed worker
  time separately. Avoid rebuilding a full checkpoint merely to learn that business contents
  have not changed, using a transactionally valid change marker/consistent snapshot boundary.
  Never reuse a checkpoint across unprotected data, policy, identity or manual-vault changes.
  Measure a complete newly prepared equivalent replay with the real S3 path, not just local ingestion.
- **P2 — bound staging lifetime.** Current persistent workspace holds a SQLite copy, another
  database-sized directory of page parts, copied originals and projected Markdown. Remove the
  redundant assembled database after the validated parts/manifest no longer need it; make
  retention/cleanup of the remaining regenerable stage explicit and lock-protected. Preserve
  repository, recovery credentials, coverage and operation journal. Handle interrupted backup,
  retry, skipped snapshots, restoration and maintenance races. Report allocated and logical size,
  retained workspace, cache and temporary peak distinctly. Do not promise zero backup peak.
- **P2 — upgrade handoff.** Ensure the installation's entry guide is refreshed with version,
  completed migration state, verification instructions and known limitations on every upgrade.
  Preserve user-specific paths/preferences and keep prior verification sections historical.
- **Measure before database compaction.** Attribute growth to evidence, equivalence/observations,
  duplicate full-text content and indexes. A zero freelist means VACUUM is not the proposed fix.
  Preserve exact source archives and citations. External-content FTS and more compact observation
  storage are separate candidates requiring rebuild/migration and recovery tests; do not mix them
  into the S3 latency fix. Adapter representation transitions with text/raw/completeness changes
  must not be silently collapsed as export metadata.

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

## Diagnostic packet status

The four requested evidence groups have been reviewed: revision pairs/wiki citation, adapter examples,
import timing/counter reports, discovery/legacy package listings. They are sufficient for the core 0.8 plan.
Only the targeted fixtures listed above remain; private originals for migration verification must be
read from an authorized consistent backup, not reconstructed from truncated diagnostic text.

The review used local files and temporary adapter checks only. No live reimport, source migration,
cleanup, access change or diagnostic upload to the public repository was performed.


## 0.9 accepted scope and limits

- Correct mixed user/context messages in derived recall, including existing immutable revisions.
  Exact text ranges distinguish human evidence from application reminders. Retain raw envelopes.
- Persist per-message, per-content, per-view-version and per-scope agent-declared reviewed/partial
  status. Missing declarations remain unseen; old chunk completion is not backfilled as full review.
- Default dialogue batches exclude service events and retain tool/parent references; all-evidence
  review remains explicit. Token estimates are approximate text-only; tool/raw bytes require reads.
- A selective finish defers remaining work; another agent resumes unread/changed records. Saving
  pages does not establish complete review. Existing pages and manual edits remain protected.
- Verify original-first-message recall, citation ranges, two-agent continuation, equivalent replay,
  true edits, S3 proof reuse/audit, restic staging cleanup and full restoration before rollout.
- SQLite compaction/external-content FTS, HTTPS/OAuth, marketplaces and richer automatic semantic
  analysis remain later work. No authentication or cloud resource changes in this release.
