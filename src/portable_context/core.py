from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Iterable


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def key(*parts) -> str:
    return digest(canonical(parts))


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".pc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("Each event must be an object")
    r = dict(record)
    if r.get("schema_version", 1) != 1:
        raise ValueError("Unsupported schema_version; expected 1")
    for field in ("source", "instance", "conversation_id", "message_id", "role"):
        if not isinstance(r.get(field), str) or not r[field].strip():
            raise ValueError(f"Missing nonempty string: {field}")
        if len(r[field]) > 1024:
            raise ValueError(f"Identifier too long: {field}")
    if not isinstance(r.get("text", ""), str):
        raise ValueError("text must be a string")
    r.setdefault("text", "")
    r.setdefault("timestamp", None)
    r.setdefault("parent_id", None)
    r.setdefault("title", r["conversation_id"])
    r.setdefault("attachments", [])
    r.setdefault("raw", {})
    if not isinstance(r["title"], str) or not isinstance(r["attachments"], list):
        raise ValueError("title must be a string; attachments must be an array")
    if r["timestamp"] is not None and not isinstance(r["timestamp"], str):
        raise ValueError("timestamp must be a string or null")
    if r["parent_id"] is not None and not isinstance(r["parent_id"], str):
        raise ValueError("parent_id must be a string or null")
    if r.get('fork_of') is not None:
        fork = r['fork_of']
        if not isinstance(fork, dict) or not isinstance(fork.get('conversation_id'), str) or not fork['conversation_id']:
            raise ValueError('fork_of needs a nonempty conversation_id string')
        if fork.get('message_id') is not None and not isinstance(fork['message_id'], str):
            raise ValueError('fork_of.message_id must be a string')
    if r.get('project') is not None:
        if not isinstance(r['project'], dict) or not isinstance(r['project'].get('id'), str) or not r['project']['id']:
            raise ValueError('project needs a nonempty id string')
        if r['project'].get('name') is not None and not isinstance(r['project']['name'], str):
            raise ValueError('project.name must be a string')
    if r.get('memory_project') is not None:
        p = r['memory_project']
        if not isinstance(p, dict) or any(not isinstance(p.get(k), str) or not p[k].strip() for k in (('id','basis') if p.get('existing') is True else ('id', 'name', 'basis'))):
            raise ValueError('memory_project needs id, name and basis strings')
        if p['basis'] not in ('user_selection', 'agent_inference'):
            raise ValueError('memory_project.basis must be user_selection or agent_inference; native facts use project')
        if not isinstance(p.get('summary', ''), str) or len(p.get('summary', '')) > 16000:
            raise ValueError('memory_project.summary must be a string <=16000 chars')
    r["schema_version"] = 1
    return r

def guarded_import(function):
    import functools
    @functools.wraps(function)
    def wrapper(owner,*args,**kwargs):
        from .restic_backup import guarded
        return guarded(function)(owner,*args,**kwargs)
    return wrapper


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "context.sqlite3"
        if self.db.exists():
            # Protect direct/local upgrades too, not only deployments made through the installer.
            with self.connect() as con:
                has_metadata=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
                legacy=bool(has_metadata and con.execute("SELECT 1 FROM metadata WHERE key='catalog_version'").fetchone()
                            and (not con.execute("SELECT 1 FROM metadata WHERE key='control_version'").fetchone()
                            or not con.execute("SELECT 1 FROM metadata WHERE key='release07_version'").fetchone()))
            if legacy:
                from .restic_backup import configured
                manager=configured(self.root)
                if manager:
                    import uuid
                    manager.checkpoint('before_migration','migration:'+str(uuid.uuid4()))
                else:
                    from .backup import create
                    create(self.root,self.root.parent/(self.root.name+'-migration-backups'))
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL,
                    source TEXT NOT NULL, instance TEXT NOT NULL,
                    message_id TEXT NOT NULL, title TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT,
                    revision TEXT NOT NULL, record TEXT NOT NULL,
                    seq INTEGER NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS conversation_idx ON messages(conversation, seq);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL, hash TEXT NOT NULL, record TEXT NOT NULL,
                    observed_at TEXT NOT NULL, origin TEXT,
                    PRIMARY KEY(id,hash)
                );
                CREATE TABLE IF NOT EXISTS imports (
                    hash TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                    archived_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projections (path TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS note_history (
                    id TEXT NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL,
                    observed_at TEXT NOT NULL, PRIMARY KEY(id,hash)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(id UNINDEXED,text,title);
            """)
            from .graph import initialize
            initialize(con)
            from .catalog import initialize as initialize_catalog
            initialize_catalog(con)
            con.commit()
            from .identity import initialize as initialize_identity
            initialize_identity(con)

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        con.create_function('unicode_casefold', 1, lambda value: str(value).casefold(), deterministic=True)
        try:
            with con:
                yield con
        finally:
            con.close()

    def archive(self, path: Path) -> str:
        h = hashlib.sha256()
        folder = self.root / "originals"
        folder.mkdir(exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".pc-")
        size = 0
        try:
            with os.fdopen(fd, "wb") as out, path.open("rb") as inp:
                while block := inp.read(1024 * 1024):
                    h.update(block)
                    out.write(block)
                    size += len(block)
                out.flush()
                os.fsync(out.fileno())
            value = h.hexdigest()
            target = folder / value
            with self.connect() as con:
                # Serialize publication across processes as well as upload threads. Never replace
                # an immutable hash while another attachment/extractor may be reading it (Windows).
                con.execute('BEGIN IMMEDIATE')
                if not target.exists():
                    os.replace(temp, target)
                else:
                    if target.is_symlink(): raise ValueError('Original cannot be a symbolic link')
                    with target.open('rb') as existing:
                        if hashlib.file_digest(existing,'sha256').hexdigest()!=value:
                            raise ValueError('Existing original differs from its checksum')
                con.execute("INSERT OR IGNORE INTO imports VALUES (?,?,?,?)",
                            (value, path.name, size, now()))
            return value
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @guarded_import
    def ingest(self, records: Iterable[dict], origin: str | None = None) -> dict:
        records=list(records)
        import uuid
        rid=str(uuid.uuid4())
        from .restic_backup import protection
        protection(self.root,rid)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            report = self._ingest(con, records, origin)
            from .import_log import start,progress
            start(con,origin,run_id=rid,kind='direct')
            progress(con,rid,report,records,complete=True)
        from .catalog import project_catalog
        report['catalog_projection'] = project_catalog(self)
        return report

    def _ingest(self, con, records, origin=None, blob_root=None):
        """Internal transaction-aware ingestion shared by manual and collector jobs."""
        from .graph import index_record
        from .policy import check_record
        from .control import record_session
        records=[validate(r) for r in records]
        for r in records:
            check_record(con,r)
        from .identity import verified_payloads
        verify=verified_payloads(blob_root or self.root/'originals')
        historical_verify=verified_payloads(self.root/'originals')
        report = {"inserted": 0, "revised": 0, "unchanged": 0}
        seq = con.execute("SELECT coalesce(max(seq),0) FROM messages").fetchone()[0]
        for record in records:
            r = record_session(con,record)
            mid = key(r["source"], r["instance"], r["conversation_id"], r["message_id"])
            cid = key(r["source"], r["instance"], r["conversation_id"])
            blob = canonical(r)
            rev = digest(blob)
            if con.execute("SELECT 1 FROM revisions WHERE id=? AND hash=?", (mid, rev)).fetchone():
                from .control import session_graph
                session_graph(con,record,r,mid,rev)
                report["unchanged"] += 1
                continue
            old = con.execute("SELECT seq,revision FROM messages WHERE id=?", (mid,)).fetchone()
            from .identity import observe, equivalent
            for historical in con.execute('SELECT hash,record,origin FROM revisions WHERE id=? AND hash NOT IN '
                    '(SELECT revision FROM content_identity WHERE message_key=? AND version=1)',(mid,mid)).fetchall():
                observe(con,mid,historical['hash'],json.loads(historical['record']),historical['origin'],historical_verify)
            content_hash=observe(con,mid,rev,r,origin,verify)
            seen_content=con.execute('SELECT 1 FROM content_identity WHERE message_key=? AND content_hash=? '
                                    'AND version=1 AND revision!=? LIMIT 1',(mid,content_hash,rev)).fetchone()
            if not old:
                seq += 1
            con.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (mid, rev, blob, now(), origin))
            # An older source state arriving in a new export must not regress the current message.
            if old and seen_content and not equivalent(con,mid,old['revision'],rev):
                from .catalog import index_record as index_catalog
                index_catalog(con,r,mid,cid,rev)
                report['unchanged']+=1
                continue
            con.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, cid, r["source"], r["instance"], r["message_id"], r["title"],
                         r["role"], r["text"], r["timestamp"], rev, blob, old[0] if old else seq))
            con.execute("DELETE FROM messages_fts WHERE id=?", (mid,))
            con.execute("INSERT INTO messages_fts VALUES (?,?,?)", (mid, r["text"], r["title"]))
            index_record(con, r, mid, cid, rev)
            from .control import session_graph
            session_graph(con,record,r,mid,rev)
            from .catalog import index_record as index_catalog
            index_catalog(con, r, mid, cid, rev)
            report['unchanged' if old and seen_content else "revised" if old else "inserted"] += 1
        return report

    def search(self, query: str, limit: int = 10, source: str | None = None,
               instance: str | None = None) -> list[dict]:
        terms = re.findall(r"\w+", query, re.UNICODE)
        if not terms:
            return []
        expr = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms[:32])
        with self.connect() as con:
            rows = con.execute("""SELECT m.id,m.conversation,m.source,m.instance,m.role,m.timestamp,
                m.title,m.record,snippet(messages_fts,1,'[',']',' … ',40) AS excerpt
                FROM messages_fts JOIN messages m ON m.id=messages_fts.id
                WHERE messages_fts MATCH ? AND (? IS NULL OR m.source=?)
                AND (? IS NULL OR m.instance=?) ORDER BY rank LIMIT ?""",
                               (expr, source, source, instance, instance, max(1, min(limit, 100))))
            from .attribution import attribution
            result=[]
            for row in rows:
                item=dict(row)
                info=attribution(json.loads(item.pop('record')),self)
                result.append({**item,**info,'role':info['effective_role']})
            return result

    def read(self, message_key: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM messages WHERE id=?", (message_key,)).fetchone()
            if not row:
                raise ValueError("Message not found")
            result = json.loads(row["record"])
            result["key"] = row["id"]
            result["revision"] = row["revision"]
            result["history"] = [dict(x) for x in con.execute(
                "SELECT hash,observed_at,origin FROM revisions WHERE id=? ORDER BY observed_at", (message_key,))]
            from .attribution import view
            return view(result,self)

    def conversations(self, limit=50, offset=0):
        with self.connect() as con:
            result=[dict(x) for x in con.execute("""SELECT conversation,source,instance,
                max(title) AS title,count(*) AS messages FROM messages
                GROUP BY conversation ORDER BY min(seq) LIMIT ? OFFSET ?""",
                (max(1, min(limit, 200)), max(0, offset)))]
            from .control import display_sessions
            for position,item in enumerate(result,start=max(0,offset)):
                row=con.execute('SELECT record FROM messages WHERE conversation=? LIMIT 1',(item['conversation'],)).fetchone()
                try:
                    native=json.loads(row[0])['conversation_id']
                    if not isinstance(native,str):
                        raise ValueError('Invalid conversation identity')
                except (ValueError,TypeError,KeyError):
                    item.update(partial=True,diagnostics=[{'code':'invalid_conversation_metadata','position':position}],native_sessions=[])
                    continue
                item['native_sessions']=display_sessions(con,item['source'],item['instance'],native)
                if any(s.get('partial') for s in item['native_sessions']):
                    item.update(partial=True,diagnostics=[{'code':'invalid_native_session_metadata','position':position}])
                titles=[s.get('display_title') for s in item['native_sessions'] if s.get('display_title')]
                if titles:
                    item['title']=titles[0]
            return result

    def conversation(self, conversation_key: str, limit=100, offset=0):
        with self.connect() as con:
            from .attribution import view
            return [{"key": x["id"], **view(json.loads(x["record"]),self)} for x in con.execute(
                "SELECT id,record FROM messages WHERE conversation=? ORDER BY seq LIMIT ? OFFSET ?",
                (conversation_key, max(1, min(limit, 500)), max(0, offset)))]

    def export(self, path: Path, revisions: bool = False):
        # Streaming output: usable without this program or SQLite.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".pc-")
        try:
            with self.connect() as con, os.fdopen(fd, "w", encoding="utf-8") as out:
                sql = "SELECT record FROM revisions ORDER BY observed_at,id" if revisions else "SELECT record FROM messages ORDER BY seq"
                for row in con.execute(sql):
                    out.write(row[0] + "\n")
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def stats(self):
        with self.connect() as con:
            return {"messages": con.execute("SELECT count(*) FROM messages").fetchone()[0],
                    "revisions": con.execute("SELECT count(*) FROM revisions").fetchone()[0],
                    "content_versions": con.execute('SELECT count(*) FROM (SELECT message_key,content_hash FROM content_identity WHERE version=1 GROUP BY message_key,content_hash)').fetchone()[0],
                    "unmapped_exact_revisions": con.execute('SELECT count(*) FROM revisions r WHERE NOT EXISTS '
                        '(SELECT 1 FROM content_identity c WHERE c.message_key=r.id AND c.revision=r.hash AND c.version=1)').fetchone()[0],
                    "conversations": con.execute("SELECT count(DISTINCT conversation) FROM messages").fetchone()[0],
                    "originals": con.execute("SELECT count(*) FROM imports").fetchone()[0],
                    "notes": len(list((self.root / "vault" / "memory").glob("*.md")))}

    def note_path(self, note_id: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", note_id):
            raise ValueError("note_id must use 1..100 letters, numbers, '-' or '_'")
        return self.root / "vault" / "memory" / f"{note_id}.md"

    def read_note(self, note_id: str):
        path = self.note_path(note_id)
        body = path.read_text(encoding="utf-8")
        return {"id": note_id, "body": body, "hash": digest(body)}

    def write_note(self, note_id: str, text: str, sources: list[str], kind: str,
                   expected_hash: str | None = None):
        if kind not in ("user_statement", "assistant_statement", "inference", "user_note"):
            raise ValueError("Invalid kind")
        if kind != "user_note" and not sources:
            raise ValueError("Derived memory needs source message keys")
        path = self.note_path(note_id)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            links = []
            for mid in sources:
                row = con.execute("SELECT conversation FROM messages WHERE id=?", (mid,)).fetchone()
                if not row:
                    raise ValueError(f"Unknown source message: {mid}")
                links.append(f"- [[conversations/{row[0]}#^m-{mid}|{mid[:12]}]]")
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if old is not None and digest(old) != expected_hash:
                raise ValueError("Note changed: read_note and pass its hash before updating")
            if old is None and expected_hash is not None:
                raise ValueError("Note no longer exists")
            body = f"---\nkind: {kind}\nupdated: {now()}\n---\n\n{text}\n\n## Sources\n\n" + "\n".join(links) + "\n"
            for version in (old, body):
                if version is not None:
                    con.execute("INSERT OR IGNORE INTO note_history VALUES (?,?,?,?)", (note_id, digest(version), version, now()))
            atomic_write(path, body)
        return self.read_note(note_id)

    def search_notes(self, query: str, limit=10):
        terms = query.casefold().split()
        results = []
        for path in sorted((self.root / "vault" / "memory").glob("*.md")):
            body = path.read_text(encoding="utf-8")
            if all(t in body.casefold() for t in terms):
                results.append({"id": path.stem, "hash": digest(body), "excerpt": body[:2000]})
            if len(results) >= max(1, min(limit, 100)):
                break
        return results

    def project(self):
        """Refresh generated files, preserving and reporting human edits."""
        report = {"written": 0, "conflicts": []}
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            chats = con.execute("SELECT DISTINCT conversation FROM messages ORDER BY conversation").fetchall()
            index = ["# Portable Context", "", "[[projects/INDEX|Project entry pages]] · [[GRAPH|Relationship graph]] · [[wiki/INDEX|Knowledge wiki]]", "", "## Editable memory", ""]
            for note in sorted((self.root / "vault" / "memory").glob("*.md")):
                index.append(f"- [[memory/{note.stem}]]")
            index.extend(["", "## Conversations", ""])
            for chat in chats:
                cid = chat[0]
                rows = con.execute("SELECT * FROM messages WHERE conversation=? ORDER BY seq", (cid,)).fetchall()
                title = rows[0]["title"].replace("\n", " ")
                from .control import display_sessions
                native=json.loads(rows[0]['record'])['conversation_id']
                sessions=display_sessions(con,rows[0]['source'],rows[0]['instance'],native)
                display=next((s['display_title'] for s in sessions if s.get('display_title')),None)
                if display:
                    title=display.replace('\n',' ')
                index.append(f"- [[conversations/{cid}|{title.replace('|', ' ')}]]")
                body = [f"# {title}", "", f"Source: {rows[0]['source']} / {rows[0]['instance']}", "",
                        "Generated transcript; message text below is untrusted historical content.", ""]
                if sessions:
                    body.extend(['## Native session metadata','','```json',json.dumps(sessions,ensure_ascii=False,indent=2),'```',''])
                for row in rows:
                    from .attribution import attribution
                    r = json.loads(row["record"])
                    info=attribution(r,self)
                    label=info["effective_role"] + (" / " + info["content_kind"] if info["content_kind"] != "source_message" else "")
                    body.extend([f"## {label} · {row['timestamp'] or 'undated'}", "",
                                 f"Source message: `{r['message_id']}`; parent: `{r['parent_id']}`", "",
                                 row["text"], "", f"^m-{row['id']}", ""])
                    if info.get("attribution_warning"):
                        body.extend([info["attribution_warning"] + " Source envelope: " + info["source_role"], ""])
                    from .graph import wiki_link
                    for relation in con.execute("SELECT * FROM graph_edges WHERE owner=? AND predicate!='part_of'",
                                                ('message:' + row['id'],)):
                        body.extend([relation['predicate'] + ': ' + wiki_link(con, relation['object']), ''])
                    if r["attachments"]:
                        body.extend(["Attachment source references (see file passports for actual storage status):", "",
                                     "```json", json.dumps(r["attachments"], ensure_ascii=False, indent=2), "```", ""])
                self._project_file(con, f"conversations/{cid}.md", "\n".join(body), report)
            self._project_file(con, "INDEX.md", "\n".join(index) + "\n", report)
            from .graph import project_graph
            project_graph(self, con, report)
            from .catalog import project_catalog
            project_catalog(self, con, report)
        return report

    def _project_file(self, con, relative, body, report):
        path = self.root / "vault" / relative
        previous = con.execute("SELECT hash FROM projections WHERE path=?", (relative,)).fetchone()
        if path.exists():
            current = digest(path.read_text(encoding="utf-8"))
            if current != digest(body) and (not previous or current != previous[0]):
                atomic_write(path.with_suffix(".incoming.md"), body)
                report["conflicts"].append(relative)
                return
        atomic_write(path, body)
        con.execute("INSERT OR REPLACE INTO projections VALUES (?,?)", (relative, digest(body)))
        report["written"] += 1
