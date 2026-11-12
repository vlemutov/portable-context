# Use your agent as the interface

Portable Context gives agents a shared workflow: the agent reasons and operates tools; the
server preserves data, jobs, typed links and wiki revisions. Start at [START-HERE](../START-HERE.md)
for the repository-link experience. The HTTPS/OAuth route is currently a `release/1.0` preview.

## 1. Connect or create your memory

The agent first checks whether you already have a memory. It helps connect that same workspace,
create one in your own Cloud.ru account, use your own server, or start in a local private folder.
An empty or temporarily unreachable memory is not a reason to create a replacement.

With terminal/file access the agent follows the [onboarding route](../skills/portable-context/references/onboarding.md).
You handle account/payment setup, private credential entry and browser/passkey consent; the agent
prepares plans, installation and checks. Cloud.ru provides the HTTPS address; a purchased domain
is unnecessary. Your account pays for the resources. A web-only chat may need a desktop agent
for installation. The skill does not by itself install MCP or sign in to your AI accounts.

Cloud.ru preview uses HTTPS/OAuth; local and existing-server stdio/SSH remain available.
The [connection guide](../skills/portable-context/references/connection.md) distinguishes verified
clients from untested ones. Every agent must reach the same live workspace, not separate copies.
The agent checks its actual MCP connection and workspace ID, recovery status and your file choices.
You then choose a first real import and read it back. Setup is not consent to export all history.

## 2. Ask for an initial import

> Save my conversations about Project Atlas, including their forks.

The agent checks available sources. There are two access routes:

- **Server-local export:** the owner allows a directory once; the agent registers an export,
  previews conversations, selects exact IDs and starts an import job.
- **Host-provided history:** the host uses its own list/read/export tools, then supplies selected
  source records through MCP. A small text export can be parsed directly by `stage_export`.

If the host cannot access account history, the agent guides you to an official export. It must not
claim that a visible prompt, a summary or one open chat is the full account history. Native history
readers for every product are not bundled. See [connector coverage](connectors.md).

Local selective jobs store only selected normalized records with their raw message fields.
They do not copy the original mixed ZIP, unselected conversations or unrelated media. Exact
whole-file retention remains available through the legacy `import` CLI when that is your intent.

The agent reports what was saved, what was already present, what changed and what was unavailable.
Known replies, forks, projects and attachment references immediately create structural links.
An interrupted job can be resumed by the same or a different connected agent.

## 3. Ask for a knowledge graph

> Read the imported discussions, build a wiki about Atlas and connect related decisions.

The agent previews review coverage and reads bounded evidence and existing wiki pages, then
submits cited updates and explicit per-revision reviews. It reports reviewed, partial, unseen and
changed evidence separately; dialogue review does not claim all tool output or file bytes were read.
See the [review contract](../skills/portable-context/references/review.md). The server commits pages,
history, links and review progress together and rejects stale writes and invented source IDs.
It does not independently verify the truth of the agent's interpretation.

The resulting vault contains:

```text
vault/
  INDEX.md                  entry point
  GRAPH.md                  typed relationship overview
  conversations/            generated source transcripts
  revisions/                exact cited source revisions, including older edits
  entities/                 projects and attachment references
  wiki/
    INDEX.md                knowledge pages
    LOG.md                  chronological operations
    SCHEMA.md               shared maintenance rules
    atlas.md                example knowledge page
  memory/                   legacy 0.1 notes, if present
```

Open the vault in Obsidian, or read the same wiki through any connected agent. Forks and replies
are structural evidence; `related_to`, `supports`, `contradicts` and `supersedes` are agent-authored
semantic relationships. Every semantic edge has cited revisions and an author label.

## 4. Continue elsewhere

> Add the new discussions and update our decisions.

The next agent connects to the same workspace, imports new records and processes changed
conversations. Unchanged source records are deduplicated. New messages arriving during analysis
remain pending for another pass. If a worker disappears, its task becomes available after the lease
expires. An outdated worker cannot overwrite a task reclaimed by another agent.

Manual wiki edits are visible to agents. They must reread and merge before updating an edited
page. Projection conflicts produce `.incoming.md` without deleting the user's file. Since an
external editor does not share database locks, avoid editing the same file at the exact instant
of a projection; keep backups/version history for the whole memory directory.

## Optional continuous capture

Initial delivery and collector delivery use the same import jobs. The producer example writes
durable batches with source IDs and request IDs; `/v1/batches` resumes a partially processed
delivery and acknowledges only after import completion. Retrying a delivered batch creates no
duplicate job or messages. The legacy `/v1/events` remains supported for 0.1 clients.

Nothing starts in the background merely because you installed the skill. The current release
does not install native capture hooks or an autonomous model worker. A configured collector can
accumulate messages while semantic enrichment remains on demand. A later model worker can claim
the same enrichment tasks; it does not require a different graph or storage format.

## What has been demonstrated

The automated acceptance test uses **two actual MCP client subprocess connections**, synthetic
history and scripted page content: client A starts a selected import and disconnects; client B
resumes, creates a wiki and later updates it after another message. It tests the protocol and
persistence, not a model's reasoning quality or a particular provider's account access.

To inspect a scripted demo locally after installation:

```sh
python examples/agent_workflow_demo.py --home /new/private/demo-directory
```

The directory must not already exist. All content in that demonstration is synthetic.
