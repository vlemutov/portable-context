# Connector coverage and setup

These are implemented format adapters, not certifications against every account export.
Tests are synthetic. Check the import report and compare counts/content with your original.

| Source | This release | Continuous collection | Gaps |
|---|---|---|---|
| Claude | `conversations*.json` in JSON/ZIP; chat_messages, content, raw fields, file references | Repeat exports | Separate saved memory/project context/instructions need separate capture; no browser recorder |
| ChatGPT | conversations JSON/ZIP, mapping nodes, all branches, raw fields | Repeat exports | No automatic web history API; saved memory not presumed included |
| Codex | session_meta + response_item JSONL, directory imports | File watcher with checkpoints | Paginated app-server history exporter not implemented; JSONL presence is not proof of complete account history |
| Gemini web | Takeout My Activity JSON; outer-cell HTML activity extraction | Repeat Takeout exports | Activity may omit responses/grouping; HTML timestamps unparsed; Gems aren't chat history |
| Telegram | Single-chat or chats.list JSON, rich text, replies, media references | New exports or native agent hook | Roles remain participants; no Telegram login/client scraping |
| Ouroboros | Generic normalized-event producer and durable HTTP outbox | Wire producer at persisted-message boundary | Native storage parsers/hooks for specific versions not yet packaged |
| Hermes / OpenClaw | MCP read/write + portable skill | Generic producer integration possible | Native provider/plugin installation not yet implemented or tested |

## Getting source exports

- **Claude:** Settings → Privacy → Export data. See [official export instructions](https://support.claude.com/en/articles/9450526-export-your-claude-data).
  Saved memory is a [separate export/import workflow](https://support.claude.com/en/articles/12123587-import-and-export-your-memory-from-claude).
- **ChatGPT:** Settings → Data controls → Export data. See [official help](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data).
- **Gemini web:** Google Takeout, My Activity → Gemini Apps, preferably JSON. Gemini/Gems are a separate category.
  See [Google's export help](https://support.google.com/gemini/answer/16920332?hl=en). Antigravity is not needed.
  If auto-selection misses localized ZIP paths, extract and import the specific activity JSON/HTML file.
- **Telegram:** Telegram Desktop → export chat history → JSON, or Settings → Advanced → Export Telegram data.
  See [Telegram export announcement](https://telegram.org/blog/export-and-more). Preserve media alongside exports or put the full folder into a ZIP.
- **Codex:** Inspect the actual installation. Local JSONL may be under `$CODEX_HOME/sessions` and
  `$CODEX_HOME/archived_sessions`; watch/import each directory using the same device instance.
  For paginated history, [app-server](https://learn.chatgpt.com/docs/app-server) requires pagination
  across threads, archived threads, turns and items. **That exporter remains future work.**

## Several Ouroboros servers

1. Install Portable Context in each server's Python environment, or call the CLI from a separate one.
2. Pick instance IDs such as `ouroboros-vps-a` and `ouroboros-home-b`.
3. Use the example producer after persistence of incoming and outgoing messages. Preserve native
   message IDs, timestamps, participants and delivery state. Capture tool events if desired.
4. Keep each outbox on durable local storage, separate from the public checkout.
5. Run a delivery worker through an SSH tunnel to the central ingest receiver; configure restart
   on boot in systemd/container orchestration as appropriate to your deployment.
6. Verify counts and a complete user→assistant exchange, then replay a batch and verify no duplicates.

[Ouroboros](https://github.com/razzant/ouroboros) has different generations, including the older
Telegram/Colab branch and the newer native/headless application. Progress JSONL output should
not be assumed to contain the complete conversation. This project intentionally does not patch
an unknown version or guess server paths. Native wiring needs a version and sample payload.

## Other agents

[Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/) can read/write
the server. Its [external memory providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers/)
offer a potential native capture point. A provider implementation is not included here.

[OpenClaw MCP](https://docs.openclaw.ai/tools/mcp) is a client path. Its
[plugin hooks](https://docs.openclaw.ai/plugins/hooks/reference) can expose conversation persistence
with appropriate access. An LLM output event alone does not prove a message was sent.
Respect the host's incognito/exclusion flags; do not bypass them with a collector.

For Codex [hooks](https://learn.chatgpt.com/docs/hooks), prefer a short queue write and independent
delivery worker. Hook availability and transcript paths vary by runtime; current JSONL watching
is a fallback, not a claim that cloud-managed sessions are covered.

## Verification before trusting completeness

Inspect oldest/newest messages, one edited message, one branched chat, one tool call, and one
attachment. Compare source counts where available. Check import errors and role uncertainty.
Archive provenance preserves what arrived; it cannot establish that a provider exported everything.
Full-text search is implemented; embeddings, automatic entity extraction and a semantic graph are
future features. Obsidian currently shows explicit note/conversation links you or an agent create.
