# Portable Context 0.9.0

A first user message prefixed by application reminders can now be recalled and cited as human
speech without attributing the reminders to the human. Existing source records, hashes and exact
citations remain immutable; attribution is a derived view. Mixed human citations use text ranges.

## Wiki review

- Dialogue scope filters service events and tool-only rows, retains bounded parent/tool context references, and bounds text
  previews. `read_evidence` verifies and pages externalized full_text too, with payload-hash citation ranges; unavailable/corrupt/over-limit text is explicitly incomplete and cannot be marked fully reviewed. exact raw revisions and attachment bytes remain
  available separately. No silent claim that tool placeholders represent the tool output.
- `review_coverage` records agent-declared reviewed/partial/unseen/changed evidence by content
  version and scope. Metadata-equivalent exports retain coverage. Legacy chunk completion does not
  create per-message declarations. View-version changes can require re-review.
- `commit_enrichment` accepts explicit reviews atomically with pages and links. Missing declarations
  do not imply reading. `finish_enrichment` closes a selective pass and preserves remaining work for
  another agent. Default selection resumes unread/changed evidence; `include_reviewed` forces review.
- Dialogue-scope completion is distinct from whole-conversation review. Coverage cannot prove model
  comprehension or semantic truth. Character-based token estimates do not price a complete analysis.

## Repeat delivery and recovery

- S3 proofs are scoped to endpoint/bucket/region/key/hash/size and expire after 24 hours by default
  (configurable 0..7 days). Receipts distinguish fresh byte verification from reuse with the prior
  date. Target changes and failed checks invalidate reuse. `verify_s3_original` forces byte audit/repair.
  First use of an old 0.8 proof requires a fresh check because its target identity was not recorded.
- Reassigning a conversation to the same project is a no-op: it no longer increments the project version or triggers a false business change.
- An unchanged pre-import checkpoint can reuse a still-existing snapshot after checking business
  rows, local original checksums, manual vault files and backup policy without rebuilding staging.
- Successful checkpoints discard regenerable staging; failed operations retain it for retry.
  Repository, recovery password, journal and verified coverage are retained. Peak temporary space
  is still required. Logical and allocated local restic size are reported separately where supported.
- Import timings separate checkpoint preparation from validation. Parallel worker durations overlap.
- Client setup updates a managed entry-guide block without discarding personal installation notes.

## Upgrade

Use the existing deployment state and client profile. Reconnect old MCP sessions. The schemas are
additive; no content-identity migration or original deletion is required for 0.9. Refresh projections
and inspect mixed-message citation warnings on existing pages. Correct a citation only after reading
its exact human segment. Do not automatically rewrite previous prose or mark old jobs fully reviewed.

Validate on a restored copy, then on the selected real conversations. No routes, authentication,
cloud resources, original history, or unrelated source selections change as part of the release.
