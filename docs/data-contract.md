# Data contract v1

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

Storage version 0.1 is additive, with no migrations yet. Back up before changing package versions.
