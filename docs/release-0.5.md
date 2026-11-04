# Portable Context 0.5

## 0.5.1 correction

Use 0.5.1 or newer. The first 0.5.0 build scoped generated payload file IDs to the block position,
so different messages could share a file passport. Message/occurrence evidence and bytes were retained,
but the logical file grouping was incorrect and transfers unnecessarily loaded a growing file history.
0.5.1 scopes payload IDs to the native message ID and reads only the filename during a transfer.

For an already prepared 0.5.0 capture, run `python -m portable_context.client upgrade-capture
--package OLD --destination NEW` (on one line), then send NEW. This preserves the immutable old
snapshot and all captured bytes. It creates corrected attachment revisions, not duplicate messages.
Old evidence remains retrievable. Unchanged captures made with 0.5.1 still replay without new revisions.

Local Claude Code discovery/capture and selective claude.ai archive capture now share the same
screened, chunked delivery workflow. Large tool payloads are archived as content-addressed files;
the transcript keeps a compact pointer. Native UUIDs stay stable across folder moves. Conflicting
copies require explicit selection. Unknown/service records remain typed evidence, not user claims.

Client: `python -m portable_context.client discover|prepare|connect|send`. Cross-platform isolated
installation: `python deploy/client_setup.py --directory <private-dir> --config <mcp-json>`.
The first transport is MCP stdio (local or SSH), with pinned workspace identity, byte checkpoints,
bounded retries, source registration and persistent completeness receipts. S3 credentials stay server-side.

Capture-v2 uses bounded record parts, declared blobs and SHA256 verification before ingestion.
Both v1/v2 server imports are supported. New captures put observation timestamps in manifests and
receipts, not message records. Rebuilding unchanged data does not revise messages; real source,
parent, metadata, capture-policy or byte changes do. Previous message/revision hashes are not rewritten.

Secret screening runs locally over structured data, embedded images and bounded text/ZIP members.
Default: block. Explicit redact mode masks matching text and quarantines suspect binary files.
This is heuristic screening, not DLP certification, OCR, encrypted archive inspection or automatic
PII removal. Opaque/partial inspection and exclusions are reported. No scanning claim is made for
data submitted directly by third-party clients bypassing prepare.

MCP additions: empty project creation/archive, paginated lint summary/detail, orphan-placeholder
cleanup preview/apply, resumable uploads and capture receipts. Existing record sources are reconciled
without modifying transcripts. Empty projects remain visible until explicitly archived.

Upgrade the server first, using the existing installer/state; it makes and verifies backups.
Old active clients must reconnect so they load the new server/tools. Test migration on a restored
copy first. Never run older writers against an upgraded database. Check message/revision identity,
project assignments, file hashes, receipt completeness and backup restore after upgrade.

Legacy omitted tool outputs and missing files cannot be recovered by a schema migration. Re-capture
selected surviving originals with the same source/instance/session/message identity. Keep legacy
revisions for citations. Cleanup removes only unmaterialized graph nodes with no remaining edges;
it never deletes source records or projects.

Limits: 32 MiB per record part/record; 512 MiB per blob; 20 GiB per MCP upload; 1 MiB chunk batches.
Large file inspection currently buffers at most one blob, so choose a client with adequate memory.
Full backup ZIPs still include originals; S3 retention is owner-controlled. Background watching,
project hierarchies, enrichment budgets and marketplace plugin packaging remain outside 0.5.
