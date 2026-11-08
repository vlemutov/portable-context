# Portable Context 0.8.0

Re-exporting a conversation can change its selected project or capture metadata without changing what
was said. 0.8 records this distinction, avoids repeated ingestion during file delivery, and requires exact
verified backup coverage before offering import-package cleanup.

## Server

- Versioned content equivalence and export observations retain exact revision archives and citations.
  Known capture/organization fields do not imply a content edit; unknown fields, redaction and omitted
  content remain conservative. External raw/text equivalence requires checksum-verified originals.
  Older state replay does not replace a newer genuine edit. Derived wiki freshness uses equivalence.
- `identity-migration` previews historical groups and affected citation owners, then applies only the
  exact reviewed plan. No source revision or original is deleted. Test on a restored copy first.
- Persisted capture phases skip committed ingestion and source parsing on transfer resumes. Checksums
  and current policy are still enforced. One verified transfer serves multiple occurrences of a blob.
  Receipts show run totals and per-call work; project snapshots also retain their initial operation totals.
- Cleanup reports both kept and removable managed paths. A full verified restore produces a sealed
  coverage inventory; package records, project occurrences and original hashes must be represented in
  both memory and a still-existing verified snapshot. Old ZIP archives with unproven coverage remain kept.
- `backup_after_import` / restic `post-import` performs a current snapshot and full temporary restore.
  Restic cache cleanup uses the operation lock. Storage reports separate cache/staging/journal; forecasts
  require explicit metadata sizes and identify logical versus allocated-size limitations.

## Client and skill

- Claude Code recognizes the supplied service families and attachment subtypes before nested content
  parsing. Default diagnostic stubs cannot expose API request copies as conversation text or tool files.
  Full diagnostics remain opt-in and respect thinking/privacy filtering. Unknown records are preserved.
- Presentation roles, task notifications, historical instruction context, file excerpts and structural
  file-history references have explicit provenance. File references do not prove original-byte availability.
- Existing project IDs can be selected during offline prepare without a fabricated project name.
- Prepare reports record-part bytes, unique blob bytes by category and package payload bytes.

## Upgrade and verification

Use the existing private deployment state with `deploy/setup.py --state STATE install`; update local
clients and installed skills, then reconnect MCP. No cloud resources, routes or authentication scheme
are changed. Runtime tables are additive; historical equivalence is an explicit separate operation.

Test identical-package replay, a newly built equivalent export, a true source edit, interruption/resume,
current-policy rejection, shared blobs, and full backup/restore coverage. Run the Windows/macOS/Linux
CI matrix; a test executed on one host is not proof of execution on another platform. Private pilot data
must remain outside the repository. See the [skill workflow](../skills/portable-context/references/stabilization.md).

`revisions` remains an exact-evidence count. `content_versions` and `unmapped_exact_revisions` distinguish
semantic history from yet-unclassified archives. This release does not physically compact source history.

HTTPS/OAuth and installation tiers remain deferred. Enrichment coverage work remains provisional 0.9.
