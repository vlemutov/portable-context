"""Agent-declared, version-scoped evidence review. Publication is not proof of reading."""
import json,re
from .core import canonical,digest,now
from .attribution import attribution,display_text

VIEW_VERSION=2
SCOPES={'dialogue','all'}


def initialize(con):
    con.execute('''CREATE TABLE IF NOT EXISTS evidence_reviews (
        message_key TEXT NOT NULL, content_hash TEXT NOT NULL, view_version INTEGER NOT NULL,
        scope TEXT NOT NULL, status TEXT NOT NULL, revision TEXT NOT NULL,
        worker TEXT NOT NULL, reviewed_at TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY(message_key,content_hash,view_version,scope))''')


def content_hash(con,ref):
    r=con.execute('SELECT content_hash FROM content_identity WHERE message_key=? AND revision=? AND version=1',
                  (ref['message_key'],ref['revision'])).fetchone()
    return r[0] if r else ref['revision']


def state(con,ref,scope):
    value=content_hash(con,ref)
    rows=con.execute('SELECT scope,status FROM evidence_reviews WHERE message_key=? AND content_hash=? AND view_version=?',
                     (ref['message_key'],value,VIEW_VERSION)).fetchall()
    relevant=[r['status'] for r in rows if r['scope'] in (scope,'all')]
    if 'reviewed' in relevant: return 'reviewed'
    if relevant: return 'partial'
    return 'changed' if con.execute('SELECT 1 FROM evidence_reviews WHERE message_key=? AND scope IN (?,?)',
                                    (ref['message_key'],scope,'all')).fetchone() else 'unseen'


def evidence(store,con,cid,scope):
    if scope not in SCOPES: raise ValueError('scope must be dialogue or all')
    items=[]
    for row in con.execute('SELECT id,revision,record FROM messages WHERE conversation=? ORDER BY seq',(cid,)):
        record=json.loads(row['record']); info=attribution(record,store)
        ref={'message_key':row['id'],'revision':row['revision']}
        text=display_text(record,info)
        substantive=re.sub(r'^\[Tool (?:call|result):[^\n]*\]\s*','',text,flags=re.MULTILINE).strip()
        included=scope=='all' or (info['effective_role']=='user' or info['effective_role']=='assistant' and bool(substantive))
        items.append({'ref':ref,'record':record,'info':info,'text':text,'included':included,'review_state':state(con,ref,scope)})
    return items


def coverage(store,conversation_keys=None,scope='dialogue'):
    with store.connect() as con:
        ids=[r[0] for r in con.execute('SELECT DISTINCT conversation FROM messages ORDER BY conversation')]
        selected=ids if conversation_keys is None else conversation_keys
        if not set(selected)<=set(ids): raise ValueError('Unknown conversation')
        reports=[]
        for cid in selected:
            items=evidence(store,con,cid,scope)
            counts={k:0 for k in ('reviewed','partial','unseen','changed','excluded')}
            for item in items: counts[item['review_state'] if item['included'] else 'excluded']+=1
            reports.append({'conversation_key':cid,'scope':scope,'total_records':len(items),**counts,
                            'selected_text_chars':sum(len(i['text']) for i in items if i['included']),
                            'approximate_text_tokens':sum((len(i['text'])+3)//4 for i in items if i['included']),
                            'estimate_note':'Text characters / 4, not a model tokenizer or cost guarantee; raw and tool payload bytes excluded.',
                            'scope_complete':counts['partial']+counts['unseen']+counts['changed']==0,
                            'whole_conversation_complete':scope=='all' and counts['reviewed']==len(items)})
    return {'conversations':reports,'basis':'agent_declared_review_of_exact_content; not proof of comprehension',
            'legacy_jobs':'Old completed chunks do not establish per-message review.'}


def batches(store,con,cid,scope,chunk_size,max_chars,include_reviewed):
    items=evidence(store,con,cid,scope)
    selected=[i for i in items if i['included'] and (include_reviewed or i['review_state']!='reviewed')]
    groups=[]
    # Keep adjacent tool exchanges together; bounded preview never returns raw tool megabytes.
    for item in selected:
        previous=groups[-1][-1] if groups else None
        is_tool=item['info']['effective_role']=='tool'
        previous_call=previous and any(a.get('kind')=='tool_use' for a in previous['record'].get('attachments',[]) if isinstance(a,dict))
        if previous and len(groups[-1])<50 and (is_tool or previous_call) and item['record'].get('parent_id')==previous['record'].get('message_id'):
            groups[-1].append(item)
        else: groups.append([item])
    out=[]; current=[]; size=0
    for group in groups:
        cost=sum(min(len(i['text']),max_chars)+400 for i in group)
        if current and (size+cost>max_chars or len(current)+len(group)>chunk_size):
            out.append(current); current=[]; size=0
        current+=group; size+=cost
    if current: out.append(current)
    native={i['record']['message_id']:i for i in items}
    def parents(group):
        refs=[]; seen={i['ref']['message_key'] for i in group}
        for i in group:
            parent=native.get(i['record'].get('parent_id'));depth=0
            while parent and parent['ref']['message_key'] not in seen and depth<8 and len(refs)<50:
                seen.add(parent['ref']['message_key']);refs.append(parent['ref']);depth+=1
                if parent['included']: break
                parent=native.get(parent['record'].get('parent_id'))
        return refs
    return [{'conversation_key':cid,'sources':[i['ref'] for i in group], 'context_refs':parents(group),
             'context_note':'Up to 8 parent steps per selected record, 50 refs per chunk. Context is not marked read by reviewing selected sources.',
             'scope':scope,'view_version':VIEW_VERSION,'max_chars':max_chars,
             'chunk':n+1,'chunks':len(out),'excluded_records':sum(not i['included'] for i in items)} for n,group in enumerate(out)]


def commit(con,payload,reviews,worker):
    """Called in the page transaction. Missing review declarations never imply read coverage."""
    if reviews is None: return {'declared':0,'legacy_unqualified':True}
    if not isinstance(reviews,list) or len(reviews)>len(payload['sources']): raise ValueError('Invalid evidence reviews')
    allowed={(r['message_key'],r['revision']) for r in payload['sources']}; seen=set()
    scope=payload.get('scope','all')
    for r in reviews:
        if not isinstance(r,dict): raise ValueError('Review entries must be objects')
        ref=(r.get('message_key'),r.get('revision'))
        if ref not in allowed or ref in seen: raise ValueError('Review must identify unique evidence in the leased chunk')
        seen.add(ref)
        if r.get('status') not in ('reviewed','partial') or not isinstance(r.get('detail'),str) or not r['detail'].strip():
            raise ValueError('Review requires reviewed/partial and a description of what was read')
        if len(r['detail'])>4000: raise ValueError('Review detail too long')
        old=state(con,r,scope)
        if old=='reviewed' and r['status']=='partial': continue
        con.execute('INSERT OR REPLACE INTO evidence_reviews VALUES (?,?,?,?,?,?,?,?,?)',
                    (ref[0],content_hash(con,r),VIEW_VERSION,scope,r['status'],ref[1],worker,now(),r['detail']))
    return {'declared':len(reviews),'scope':scope}


def finish(workspace,job_id,reason):
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('Explain the selective stopping point')
    with workspace.store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        job=con.execute("SELECT * FROM jobs WHERE id=? AND kind='enrichment'",(job_id,)).fetchone()
        if not job: raise ValueError('Unknown enrichment job')
        import time
        if con.execute("SELECT 1 FROM job_items WHERE job_id=? AND state='leased' AND lease_until>?",(job_id,time.time())).fetchone():
            raise ValueError('Commit active leases or wait for expiry before closing selective review')
        con.execute("UPDATE job_items SET state='deferred' WHERE job_id=? AND state IN ('pending','leased')",(job_id,))
        details=json.loads(job['details']); details['selective_finish_reason']=reason
        con.execute("UPDATE jobs SET status='completed_selective',details=?,updated_at=? WHERE id=?",(canonical(details),now(),job_id))
    return {'job':workspace.job(job_id),'coverage':coverage(workspace.store,list(details.get('target_fingerprints',{})),
                                                           json.loads(job['specification']).get('scope','all'))}
