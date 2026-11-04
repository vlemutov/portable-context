"""Append-only import lifecycle events with a rebuildable progress view; never copy source text."""
import json
import uuid
from .core import canonical,now,key


def executor(value=None):
    value=value or {}
    if not isinstance(value,dict) or set(value)-{'client','version','agent'}:
        raise ValueError('Executor accepts client, version, agent labels')
    if any(v is not None and (not isinstance(v,str) or len(v)>200) for v in value.values()):
        raise ValueError('Executor labels must be <=200 characters')
    return {'client':value.get('client') or 'unknown','version':value.get('version'),
            'agent':value.get('agent'),'identity_basis':'sender_reported'}


def start(con,package_id=None,who=None,run_id=None,kind='capture'):
    if run_id is not None and (not isinstance(run_id,str) or not 1<=len(run_id)<=200):
        raise ValueError('Import run ID must be 1..200 characters')
    rid=run_id or str(uuid.uuid4())
    existing=con.execute('SELECT package_id FROM import_runs WHERE id=?',(rid,)).fetchone()
    if existing:
        if existing[0]!=package_id:
            raise ValueError('Import run belongs to another package')
        return rid
    details={'executor':executor(who),'kind':kind,'messages':dict(inserted=0,revised=0,unchanged=0)}
    at=now()
    con.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)',(rid,at,at,'in_progress',package_id,canonical(details)))
    con.execute('INSERT INTO import_events(run_id,at,event,details) VALUES (?,?,?,?)',(rid,at,'started',canonical(details)))
    return rid


def progress(con,run_id,counts,records,complete=False,receipt=None,error=None,accumulate=False,batch_key=None):
    row=con.execute('SELECT * FROM import_runs WHERE id=?',(run_id,)).fetchone()
    if not row:
        raise ValueError('Unknown import run')
    if row['status']=='complete':
        return
    details=json.loads(row['details'])
    if batch_key:
        if con.execute('SELECT 1 FROM import_run_batches WHERE run_id=? AND batch_key=?',(run_id,batch_key)).fetchone():
            return
        con.execute('INSERT INTO import_run_batches VALUES (?,?)',(run_id,batch_key))
    if accumulate:
        details['messages']={k:details['messages'][k]+counts[k] for k in counts}
    elif not details.get('records_seen') and not error:
        details['messages']=counts
    details['records_seen']=details.get('records_seen',False) or bool(records)
    details['sources']=sorted({tuple(s) for s in details.get('sources',[])}|{(r['source'],r['instance']) for r in records})
    details['conversations']=sorted(set(details.get('conversations',[]))|{key(r['source'],r['instance'],r['conversation_id']) for r in records})
    project_ids=set(details.get('projects',[]))
    current=sorted({key(r['source'],r['instance'],r['conversation_id']) for r in records})
    for start_index in range(0,len(current),500):
        chunk=current[start_index:start_index+500]
        project_ids.update(r[0] for r in con.execute('SELECT project_id FROM catalog_assignments WHERE conversation IN ('+
            ','.join('?' for _ in chunk)+')',chunk))
    details['projects']=sorted(project_ids)
    if receipt:
        from .transfer import compact_receipt
        compact=compact_receipt(receipt)
        details['delivery']={k:compact.get(k) for k in ('stored_attachment_occurrences','s3','remaining_transfers','completeness')}
        kinds={'document':0,'source_payload':0}
        for attachment in receipt.get('attachments',[]):
            r=con.execute('SELECT category FROM catalog_occurrences WHERE id=?',(attachment['occurrence_id'],)).fetchone()
            if r:
                kinds[r[0]]+=1
        details['stored_by_category']=kinds
    status='failed' if error else ('complete' if complete else 'in_progress')
    if error:
        details['error']=error
    else:
        details.pop('error',None)
    at=now()
    con.execute('UPDATE import_runs SET updated_at=?,status=?,details=? WHERE id=?',(at,status,canonical(details),run_id))
    if complete or error:
        con.execute('INSERT INTO import_events(run_id,at,event,details) VALUES (?,?,?,?)',
                    (run_id,at,status,canonical(details)))


def compact(details):
    result=dict(details)
    for field in ('conversations','projects','sources'):
        if field in result:
            result[field+'_count']=len(result[field])
            result[field]=result[field][:20]
    result['scope_preview_limit']=20
    return result


def list_imports(store,limit=20,offset=0):
    with store.connect() as con:
        return [{**dict(r),'details':compact(json.loads(r['details']))} for r in con.execute(
            'SELECT * FROM import_runs ORDER BY started_at DESC,id LIMIT ? OFFSET ?',
            (max(1,min(limit,100)),max(0,offset)))]


def events(store,run_id,limit=50,offset=0):
    with store.connect() as con:
        return [{**dict(r),'details':compact(json.loads(r['details']))} for r in con.execute(
            'SELECT * FROM import_events WHERE run_id=? ORDER BY seq LIMIT ? OFFSET ?',
            (run_id,max(1,min(limit,100)),max(0,offset)))]


def scope(store,run_id,field='conversations',limit=50,offset=0):
    if field not in ('conversations','projects','sources'):
        raise ValueError('Select conversations, projects or sources')
    with store.connect() as con:
        row=con.execute('SELECT details FROM import_runs WHERE id=?',(run_id,)).fetchone()
    if not row:
        raise ValueError('Unknown import run')
    values=json.loads(row[0]).get(field,[])
    offset=max(0,offset); limit=max(1,min(limit,100))
    return {'field':field,'total':len(values),'items':values[offset:offset+limit],
            'next_offset':offset+limit if offset+limit<len(values) else None}


def backfill(con):
    if con.execute("SELECT 1 FROM metadata WHERE key='import_log_backfill'").fetchone():
        return
    for row in con.execute('SELECT * FROM capture_receipts ORDER BY updated_at,id').fetchall():
        report=json.loads(row['report'])
        rid='legacy:'+row['id']
        details={'executor':executor(),'kind':'legacy_capture','historical_time_basis':'latest_preserved_receipt',
                 'messages':report.get('initial_messages',report.get('messages',{})),
                 'completeness':report.get('completeness',{}),'legacy_status':report.get('status')}
        con.execute('INSERT OR IGNORE INTO import_runs VALUES (?,?,?,?,?,?)',
                    (rid,row['updated_at'],row['updated_at'],'complete' if report.get('complete') else 'legacy_incomplete',row['id'],canonical(details)))
        con.execute('INSERT INTO import_events(run_id,at,event,details) VALUES (?,?,?,?)',
                    (rid,row['updated_at'],'legacy_receipt_observed',canonical(details)))
    for row in con.execute("SELECT * FROM jobs WHERE kind='import' ORDER BY created_at,id").fetchall():
        if con.execute('SELECT 1 FROM import_runs WHERE id=?',(row['id'],)).fetchone():
            continue
        counts=dict(inserted=0,revised=0,unchanged=0)
        for item in con.execute('SELECT result FROM job_items WHERE job_id=? AND result IS NOT NULL',(row['id'],)):
            r=json.loads(item[0])
            for k in counts:
                counts[k]+=r.get(k,0)
        details={'executor':executor(),'kind':'legacy_import_job','source_id':row['source_id'],
                 'messages':counts,'historical_time_basis':'preserved_job','records_seen':True}
        con.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)',(row['id'],row['created_at'],row['updated_at'],
                    'complete' if row['status']=='completed' else 'legacy_incomplete','job:'+row['id'],canonical(details)))
        con.execute('INSERT INTO import_events(run_id,at,event,details) VALUES (?,?,?,?)',
                    (row['id'],row['updated_at'],'legacy_job_observed',canonical(details)))
    con.execute("INSERT INTO metadata VALUES ('import_log_backfill','1')")
