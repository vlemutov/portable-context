# Portable Context 0.6.0

Documents, source payloads, capture permissions and import provenance are now distinct controls.
This release includes project groups and conservative native-session identity handling. Automatic
enrichment budgeting/client setup/marketplace packaging are planned for 0.7; automatic Claude Code
delivery is planned separately. No background collection or model calls are enabled by this upgrade.

## Upgrade

1. Back up the entire workspace with the old version and verify restoration. Test migration on a copy.
2. Upgrade the server with the existing deployment state (`deploy/setup.py --state STATE install`).
3. Upgrade the local client from this release (`deploy/client_setup.py --directory CLIENT --config CONFIG`).
4. Replace installed skill copies in Claude Code/Codex; keep backups outside scanned skill directories.
   Upload `portable-context-skill-0.6.0.zip` for a manually installed claude.ai skill.
5. Reconnect persistent MCP processes. Check the same workspace ID, 0.6.0 and required capabilities.
   Existing SSH/stdio connection settings and S3 placement do not need changing.

The client requires 0.6 server capabilities before delivery. Existing capture-v1/v2 packages remain
readable/importable when compatible with effective server policies. Legacy packages without scope
declarations are refused in policy-restricted memories. Rebuild locally rather than bypassing checks.
Do not run old writer processes against an upgraded workspace. No existing source is reimported as
part of schema migration; no existing original or source revision is deleted or rehashed.

## Changes and intentional limits

- Default file lists/search and project entries show current documents. Tool/raw copies remain
  addressable by exact passport, occurrence and byte hash. Unmodified generated payload Markdown
  pages are retired; manual edits are preserved as projection conflicts. Source history stays intact.
- Counts distinguish current vs historical observations and unique original bytes. Category bytes
  can overlap; do not add them to estimate total disk use. Backups/staging/database are additional.
- Known runtime paths are excluded on future captures, with explicit reasons and exact-path override.
  Arbitrary OS temporary files are not blanket-excluded. Prior captured runtime data is retained.
- Local policies control conversation, tool/raw payloads and document bytes separately; server policy
  intersects workspace/project permissions before upload and on ingestion. Retained prose can contain
  sensitive data; these controls are not semantic redaction or an adversarial-client security boundary.
- Groups have stable IDs, versioned rename/membership and Markdown entry pages. They are organizational.
  Moving description prefixes into groups is previewed; no automatic personal/work classification.
- Import runs retain per-operation counts and sender-reported executor. Lifecycle events are append-only;
  materialized progress and delivery receipts can change. Historical unknown actors remain unknown.
- Mixed native session IDs are discovered/selected independently. Relocation/ID changes are observations,
  not inferred forks. Title history is stored separately, so renaming does not revise all messages.
  Existing legacy grouping aliases preserve citations; ambiguous identity is rejected for review.

See [controls](../skills/portable-context/references/controls.md) for exact policy resolution and tools.
CI uses synthetic fixtures only, including old-schema migration, repeated delivery, policy changes,
shared payload bytes, native-session transitions, title changes and append-only journal behavior.
