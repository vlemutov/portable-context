"""Versioned catalog controls; source records and revision hashes are never migrated in place."""
import json
from .core import canonical, key, now

PAYLOAD_KINDS = {'service_event', 'tool_use', 'tool_result', 'full_raw', 'full_text',
                 'thinking', 'redacted_thinking'}


def category(metadata):
    if metadata.get('category') in ('document', 'source_payload'):
        return metadata['category']
    if metadata.get('kind') == 'image':
        return 'document'
    return 'source_payload' if (metadata.get('kind') in PAYLOAD_KINDS or
        str(metadata.get('file_id', '')).startswith('payload:')) else 'document'


def initialize(con):
    # Called inside catalog's migration transaction. Do not use executescript (implicit commit).
    for sql in (
        'CREATE TABLE IF NOT EXISTS project_groups (id TEXT PRIMARY KEY,title TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS capture_policies (target TEXT PRIMARY KEY,policy TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS source_sessions (source TEXT NOT NULL,instance TEXT NOT NULL,native_id TEXT NOT NULL,conversation_id TEXT NOT NULL,details TEXT NOT NULL,PRIMARY KEY(source,instance,native_id))',
        'CREATE TABLE IF NOT EXISTS import_runs (id TEXT PRIMARY KEY,started_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,package_id TEXT,details TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS import_events (seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,at TEXT NOT NULL,event TEXT NOT NULL,details TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS import_run_batches (run_id TEXT NOT NULL,batch_key TEXT NOT NULL,PRIMARY KEY(run_id,batch_key))',
        "CREATE TRIGGER IF NOT EXISTS immutable_import_events_update BEFORE UPDATE ON import_events BEGIN SELECT RAISE(ABORT,'Import events are append-only'); END",
        "CREATE TRIGGER IF NOT EXISTS immutable_import_events_delete BEFORE DELETE ON import_events BEGIN SELECT RAISE(ABORT,'Import events are append-only'); END",
    ):
        con.execute(sql)
    for table, column, definition in (
        ('catalog_occurrences', 'category', "TEXT NOT NULL DEFAULT 'document'"),
        ('catalog_projects', 'group_id', 'TEXT'),
        ('project_groups', 'identifier', 'TEXT'),
    ):
        if column not in {r[1] for r in con.execute('PRAGMA table_info('+table+')')}:
            con.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+definition)
    if con.execute("SELECT 1 FROM metadata WHERE key='control_version'").fetchone():
        return
    for row in con.execute('SELECT id,metadata FROM catalog_occurrences').fetchall():
        con.execute('UPDATE catalog_occurrences SET category=? WHERE id=?',
                    (category(json.loads(row['metadata'])),row['id']))
    # Preserve legacy grouping explicitly, including mixed-native-session files from 0.5.
    for row in con.execute("SELECT record FROM messages WHERE source='claude-code' ORDER BY seq").fetchall():
        r=json.loads(row[0])
        native=(r.get('raw') or {}).get('sessionId') or r['conversation_id']
        if '/' in r['conversation_id']:
            native=str(native)+'/'+r['conversation_id'].split('/',1)[1]
        existing=con.execute('SELECT conversation_id FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                             (r['source'],r['instance'],native)).fetchone()
        if existing and existing[0]!=r['conversation_id']:
            # Never silently merge conflicting historical identities.
            details={'identity_conflict':True,'legacy_groups':sorted({existing[0],r['conversation_id']})}
            con.execute('UPDATE source_sessions SET details=? WHERE source=? AND instance=? AND native_id=?',
                        (canonical(details),r['source'],r['instance'],native))
        else:
            con.execute('INSERT OR IGNORE INTO source_sessions VALUES (?,?,?,?,?)',
                        (r['source'],r['instance'],native,r['conversation_id'],canonical({'legacy_grouping':native!=r['conversation_id']})))
            prior=con.execute('SELECT details FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                              (r['source'],r['instance'],native)).fetchone()
            details=json.loads(prior[0])
            raw=r.get('raw') or {}
            for field in ('customTitle','aiTitle'):
                if raw.get(field):
                    title={'value':str(raw[field]),'kind':field,'timestamp':r.get('timestamp'),'message_id':r['message_id']}
                    details.setdefault('titles',[])
                    if title not in details['titles']:
                        details['titles'].append(title)
            if raw.get('relocatedCwd') or raw.get('type')=='relocated':
                observation={'cwd':raw.get('cwd'),'relocated_cwd':raw.get('relocatedCwd'),
                             'timestamp':r.get('timestamp'),'message_id':r['message_id']}
                details.setdefault('relocations',[])
                if observation not in details['relocations']:
                    details['relocations'].append(observation)
            titles=[t for t in details.get('titles',[]) if t['kind']=='customTitle'] or details.get('titles',[])
            if titles:
                details['display_title']=titles[-1]['value']
            con.execute('UPDATE source_sessions SET details=? WHERE source=? AND instance=? AND native_id=?',
                        (canonical(details),r['source'],r['instance'],native))
    con.execute("INSERT INTO metadata VALUES ('control_version','1')")


def group(con, group_id):
    if group_id is not None and not con.execute('SELECT 1 FROM project_groups WHERE id=?',(group_id,)).fetchone():
        raise ValueError('Unknown project group')


def record_session(con, record):
    """Resolve observed native identities to stable memory identities before computing a revision."""
    r=dict(record)
    details=r.pop('conversation_metadata',None)
    if r.get('source')!='claude-code' or details is None:
        return r
    native=details.get('native_id',r['conversation_id'])
    prior=con.execute('SELECT * FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                      (r['source'],r['instance'],native)).fetchone()
    old=json.loads(prior['details']) if prior else {}
    if not prior and not r.get('fork_of'):
        existing=con.execute('SELECT record FROM messages WHERE source=? AND instance=? AND message_id=?',
                             (r['source'],r['instance'],r['message_id'])).fetchall()
        if any(json.loads(row[0])['conversation_id']!=native for row in existing):
            raise ValueError('Unmapped existing message UUID in another session; explicit identity reconciliation required')
    if old.get('identity_conflict'):
        raise ValueError('Ambiguous legacy session identity; explicit reconciliation required')
    cid=prior['conversation_id'] if prior else native
    r['conversation_id']=cid
    merged={**old,**details}
    for field in ('titles','relocations','transitions'):
        merged[field]=list(old.get(field,[]))
        merged[field]+=[x for x in details.get(field,[]) if x not in merged[field]]
    titles=[t for t in merged['titles'] if t['kind']=='customTitle'] or merged['titles']
    if titles:
        merged['display_title']=titles[-1]['value']
    con.execute('INSERT OR REPLACE INTO source_sessions VALUES (?,?,?,?,?)',
                (r['source'],r['instance'],native,cid,canonical(merged)))
    # The display title lives in session metadata; renaming does not revise every message.
    mid=key(r['source'],r['instance'],cid,r['message_id'])
    current=con.execute('SELECT title FROM messages WHERE id=?',(mid,)).fetchone()
    r['title']=current[0] if current else cid
    return r


def display_sessions(con,source,instance,conversation_id):
    result=[]
    for r in con.execute(
        'SELECT native_id,details FROM source_sessions WHERE source=? AND instance=? AND conversation_id=? ORDER BY native_id',
        (source,instance,conversation_id)):
        try:
            details=json.loads(r['details'])
            if not isinstance(details,dict) or (details.get('display_title') is not None
                                               and not isinstance(details['display_title'],str)):
                raise ValueError('Invalid display metadata')
            result.append({**details,'native_id':r['native_id']})
        except (ValueError,TypeError):
            result.append({'native_id':r['native_id'],'partial':True,
                           'diagnostic':{'code':'invalid_native_session_metadata'}})
    return result


def session_graph(con,original,record,mid,revision):
    meta=original.get('conversation_metadata')
    if not meta:
        return
    from .graph import node,edge
    prefix=(record['source'],record['instance'])
    native=meta.get('native_id',original['conversation_id'])
    saved=con.execute('SELECT details FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                      (*prefix,native)).fetchone()
    if saved:
        meta=json.loads(saved[0])
    sid='native-session:'+key(*prefix,native)
    cid='conversation:'+key(*prefix,record['conversation_id'])
    evidence=[{'message_key':mid,'revision':revision}]
    node(con,sid,'source_session',meta.get('display_title',native),meta)
    edge(con,cid,'contains_native_session',sid,'structural',sid,evidence,'source')
    for transition in meta.get('transitions',[]):
        parent='native-session:'+key(*prefix,transition['from'])
        node(con,parent,'source_session',transition['from'],{'native_id':transition['from']},False)
        edge(con,sid,'observed_after_session',parent,'structural',sid,evidence,'source')


def list_sessions(store, source=None, limit=30, offset=0):
    with store.connect() as con:
        return [{**dict(r),'details':json.loads(r['details'])} for r in con.execute(
            'SELECT * FROM source_sessions WHERE (? IS NULL OR source=?) ORDER BY source,instance,native_id LIMIT ? OFFSET ?',
            (source,source,max(1,min(limit,100)),max(0,offset)))]
