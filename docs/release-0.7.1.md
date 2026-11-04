# Portable Context 0.7.1

This patch repairs conversation listing, Claude Code source attribution and workspace discovery.
It does not migrate revision hashes, rewrite source history or change accepted folder bindings.

## Server

- `list_conversations` accepts duplicate native_id metadata and preserves legacy grouping. A damaged
  metadata row returns its stable conversation/native ID, `partial=true` and a bounded diagnostic;
  other rows and offset pagination remain readable. Database failures still fail explicitly.
- Recall, search, graph reads, enrichment previews and generated Markdown distinguish Claude compact
  summaries using the explicit boolean `isCompactSummary`. Visibility-only flags do not imply summaries.
  The effective role is `assistant`, content kind `generated_summary`; `source_role` preserves the source
  envelope. These are derived views, not new source versions. Do not reimport recall views as source exports.
- Exact revision JSON and JSONL exports remain unchanged; revision reads include separate attribution.
  Archived raw flags are resolved only from checksum-verified local originals up to 8 MiB. Missing,
  oversized or unverified raw payloads produce unknown attribution, never a claimed human statement.
- Existing user_statement wiki pages citing summaries are flagged for review in reads/search/lint and
  the wiki index. Their content/history is preserved. New such claims need direct user evidence, or
  an appropriate inference/assistant_statement kind. This check cannot assess arbitrary prose semantics.

## Local client and skill

- Discovery accumulates cwd/relocatedCwd observations across all records, including late cwd values;
  copied transcripts are counted by distinct native record identity. Actual cwd counts and relocation
  target counts are separate. Local path status distinguishes missing, unreadable and foreign-platform.
- Several workspaces sharing an encoded Claude directory make its adjacent memory folder ambiguous.
  `memory_candidates` contains the evidence; ambiguous roots are withheld from `memory_roots` suggestions.
  Discovery should cover the full projects root, since a selected subset cannot prove global ownership.
- The skill explains effective attribution and requires an ownership choice before binding ambiguous
  folders. Existing accepted bindings are not changed automatically.

## Upgrade and validation

Use the same private deployment state with `deploy/setup.py --state STATE install`, then update local
client and installed skill copies. Managed installation verifies a restic backup/restore before switching
the active release. Reconnect persistent MCP clients. Connection settings and workspace identity stay valid.
Refresh generated vault views after the upgrade; manual edits keep the usual conflict protection.

Regression coverage includes the duplicate-key failure, corrupt metadata and pagination, legacy alias
display, no source revision changes through recall/replay, compact versus visibility-only flags, archived
raw checksum failure, old/new wiki claims, late cwd, copied sessions, relocation and shared directory
collisions. Run the complete test suite and the existing Windows/macOS/Linux CI matrix before calling
each platform verified. Private pilot transcripts are not included in the repository.

Next: [0.8 content identity/import/storage stabilization and 0.9 enrichment](backlog-next.md).
