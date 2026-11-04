"""Typed, provenance-bearing graph. Structural edges come only from source fields."""
import json

from .core import canonical, digest, key, now


def initialize(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
            data TEXT NOT NULL, materialized INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS graph_edges (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, layer TEXT NOT NULL, owner TEXT NOT NULL,
            evidence TEXT NOT NULL, author TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS graph_subject ON graph_edges(subject);
        CREATE INDEX IF NOT EXISTS graph_object ON graph_edges(object);
        CREATE INDEX IF NOT EXISTS graph_owner ON graph_edges(owner);
    """)
    con.execute("BEGIN IMMEDIATE")
    if not con.execute("SELECT 1 FROM metadata WHERE key='graph_version'").fetchone():
        for row in con.execute("SELECT id,conversation,revision,record FROM messages").fetchall():
            index_record(con, json.loads(row['record']), row['id'], row['conversation'], row['revision'])
        con.execute("INSERT INTO metadata VALUES ('graph_version','1')")


def node(con, identifier, kind, label, data=None, materialized=True):
    con.execute("""INSERT INTO graph_nodes VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        label=CASE WHEN excluded.materialized THEN excluded.label ELSE graph_nodes.label END,
        data=CASE WHEN excluded.materialized THEN excluded.data ELSE graph_nodes.data END,
        materialized=max(graph_nodes.materialized,excluded.materialized)""",
        (identifier, kind, str(label), canonical(data or {}), int(materialized)))


def edge(con, subject, predicate, obj, layer, owner, evidence, author):
    identifier = key(layer, owner, subject, predicate, obj)
    con.execute("INSERT OR REPLACE INTO graph_edges VALUES (?,?,?,?,?,?,?,?,?)",
                (identifier, subject, predicate, obj, layer, owner, canonical(evidence), author, now()))


def index_record(con, r, mid, cid, revision):
    message = 'message:' + mid
    chat = 'conversation:' + cid
    prefix = (r['source'], r['instance'])
    node(con, chat, 'conversation', r['title'], {'source': r['source'], 'instance': r['instance'],
                                              'conversation_id': r['conversation_id']})
    node(con, message, 'message', r['text'][:100] or r['role'],
         {'conversation': cid, 'message_key': mid, 'revision': revision, 'role': r['role']})
    con.execute("DELETE FROM graph_edges WHERE owner=? AND layer='structural'", (message,))
    evidence = [{'message_key': mid, 'revision': revision}]

    def link(a, predicate, b):
        edge(con, a, predicate, b, 'structural', message, evidence, 'source')

    link(message, 'part_of', chat)
    if isinstance(r.get('subagent_of'),dict):
        parent_id=r['subagent_of'].get('conversation_id')
        if isinstance(parent_id,str) and parent_id:
            ancestor='conversation:'+key(*prefix,parent_id)
            node(con,ancestor,'conversation',parent_id,{'conversation_id':parent_id},False)
            link(chat,'subagent_of',ancestor)
    if r.get('parent_id'):
        parent_key = key(*prefix, r['conversation_id'], r['parent_id'])
        if not con.execute('SELECT 1 FROM messages WHERE id=?',(parent_key,)).fetchone():
            candidates=con.execute('SELECT id FROM messages WHERE source=? AND instance=? AND message_id=?',
                                   (*prefix,r['parent_id'])).fetchall()
            if len(candidates)==1:
                parent_key=candidates[0][0]
        parent = 'message:' + parent_key
        node(con, parent, 'message', r['parent_id'], {'conversation': cid, 'message_key': parent_key}, False)
        link(message, 'replies_to', parent)
    fork = r.get('fork_of')
    if fork:
        if not isinstance(fork, dict) or not isinstance(fork.get('conversation_id'), str):
            raise ValueError('fork_of needs a source-native conversation_id string')
        ancestor_key = key(*prefix, fork['conversation_id'])
        ancestor = 'conversation:' + ancestor_key
        node(con, ancestor, 'conversation', fork['conversation_id'],
             {'source': prefix[0], 'instance': prefix[1], 'conversation_id': fork['conversation_id']}, False)
        link(chat, 'forked_from', ancestor)
        if fork.get('message_id'):
            anchor_key = key(*prefix, fork['conversation_id'], str(fork['message_id']))
            anchor = 'message:' + anchor_key
            node(con, anchor, 'message', fork['message_id'],
                 {'conversation': ancestor_key, 'message_key': anchor_key}, False)
            link(chat, 'forked_at', anchor)
    project = r.get('project')
    if project:
        if not isinstance(project, dict) or not isinstance(project.get('id'), str):
            raise ValueError('project needs an id string')
        project_key = 'project:' + key(*prefix, project['id'])
        node(con, project_key, 'project', project.get('name', project['id']), project)
        link(chat, 'in_project', project_key)
    for attachment in r['attachments']:
        # References only. Never fetch an arbitrary URL or execute an attachment.
        file_key = 'attachment:' + key(cid, canonical(attachment))
        label = (attachment.get('file_name') or attachment.get('path') or attachment.get('name') or 'Attachment') if isinstance(attachment, dict) else str(attachment)
        node(con, file_key, 'attachment', label, {'reference': attachment})
        link(message, 'has_attachment', file_key)


def evidence_stale(con, evidence):
    for ref in evidence:
        row = con.execute('SELECT revision FROM messages WHERE id=?', (ref['message_key'],)).fetchone()
        if not row or row[0] != ref['revision']:
            return True
    return False


def read_graph(store, node_id=None, layer=None, limit=100, offset=0):
    if layer not in (None, 'structural', 'semantic'):
        raise ValueError('layer must be structural or semantic')
    with store.connect() as con:
        rows = con.execute("""SELECT * FROM graph_edges WHERE (? IS NULL OR layer=?)
            AND (? IS NULL OR subject=? OR object=?) ORDER BY id LIMIT ? OFFSET ?""",
            (layer, layer, node_id, node_id, node_id, max(1, min(limit, 500)), max(0, offset))).fetchall()
        edges = []
        ids = set()
        for row in rows:
            item = dict(row)
            item['evidence'] = json.loads(item['evidence'])
            item['stale'] = evidence_stale(con, item['evidence'])
            edges.append(item)
            ids.update((item['subject'], item['object']))
        if node_id:
            ids.add(node_id)
        nodes = []
        for identifier in sorted(ids):
            row = con.execute('SELECT * FROM graph_nodes WHERE id=?', (identifier,)).fetchone()
            if row:
                nodes.append({**dict(row), 'data': json.loads(row['data']), 'materialized': bool(row['materialized'])})
        return {'nodes': nodes, 'edges': edges, 'next_offset': offset + len(rows) if len(rows) == max(1, min(limit, 500)) else None}


def payload_node(con,identifier):
    row=con.execute('SELECT kind,data FROM graph_nodes WHERE id=?',(identifier,)).fetchone()
    if not row:
        return False
    if row['kind']=='attachment':
        from .control import category
        reference=json.loads(row['data']).get('reference')
        return isinstance(reference,dict) and category(reference)=='source_payload'
    if row['kind']=='file':
        return not con.execute("SELECT 1 FROM catalog_occurrences WHERE file_id=? AND category='document'",
                               (identifier.split(':',1)[1],)).fetchone()
    return False


def wiki_link(con, identifier):
    row = con.execute('SELECT * FROM graph_nodes WHERE id=?', (identifier,)).fetchone()
    if not row:
        return '`' + identifier + '`'
    label = row['label'].replace('|', ' ').replace('[', '(').replace(']', ')').replace('\n', ' ')[:100]
    if payload_node(con,identifier):
        return '`Source payload: '+label+'` (retrieve the exact occurrence through MCP)'
    data = json.loads(row['data'])
    if row['kind'] == 'conversation':
        path = 'conversations/' + identifier.split(':', 1)[1]
    elif row['kind'] == 'message':
        path = 'conversations/' + data['conversation'] + '#^m-' + data['message_key']
    elif row['kind'] == 'wiki':
        path = 'wiki/' + identifier.split(':', 1)[1]
    elif row['kind'] == 'file':
        path = 'files/' + identifier.split(':', 1)[1]
    else:
        path = 'entities/' + digest(identifier)
    return '[[' + path + '|' + label + ']]'


def project_graph(store, con, report):
    index = ['# Graph', '', 'Structural relationships come from source fields. Semantic relationships are agent-authored.', '']
    for row in con.execute("SELECT * FROM graph_nodes WHERE kind IN ('project','attachment','source_session') ORDER BY id"):
        if payload_node(con,row['id']):
            rel='entities/'+digest(row['id'])+'.md'
            path=store.root/'vault'/rel
            previous=con.execute('SELECT hash FROM projections WHERE path=?',(rel,)).fetchone()
            if path.exists():
                if previous and digest(path.read_text(encoding='utf-8'))==previous[0]:
                    path.unlink()
                    con.execute('DELETE FROM projections WHERE path=?',(rel,))
                else:
                    report['conflicts'].append(rel)
            continue
        body = ['# ' + row['label'].replace('\n', ' '), '', '```json', row['data'], '```', '', '## Connections', '']
        for e in con.execute('SELECT * FROM graph_edges WHERE subject=? OR object=?', (row['id'], row['id'])):
            body.append('- ' + wiki_link(con, e['subject']) + ' — ' + e['predicate'] + ' → ' + wiki_link(con, e['object']))
        store._project_file(con, 'entities/' + digest(row['id']) + '.md', '\n'.join(body) + '\n', report)
    # Relationships between messages remain in the machine-readable graph; keep overview compact.
    for e in con.execute("SELECT * FROM graph_edges WHERE predicate NOT IN ('part_of','replies_to','has_attachment') ORDER BY layer,id"):
        if payload_node(con,e['subject']) or payload_node(con,e['object']):
            continue
        stale = ' [source changed]' if evidence_stale(con, json.loads(e['evidence'])) else ''
        index.append('- ' + wiki_link(con, e['subject']) + ' — ' + e['predicate'] + ' → ' + wiki_link(con, e['object']) + stale)
    store._project_file(con, 'GRAPH.md', '\n'.join(index) + '\n', report)
