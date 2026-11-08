"""Versioned, conservative content equivalence; exact source archives are never rewritten."""
import json
from .core import canonical, digest, now

VERSION = 1
OBSERVATIONS = {'capture_context', 'memory_project', 'capture_policy', 'conversation_metadata'}


def initialize(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS content_identity (
            message_key TEXT NOT NULL, revision TEXT NOT NULL, version INTEGER NOT NULL,
            content_hash TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(message_key,revision,version));
        CREATE INDEX IF NOT EXISTS content_identity_hash ON content_identity(message_key,content_hash,version);
        CREATE TABLE IF NOT EXISTS export_observations (
            message_key TEXT NOT NULL, revision TEXT NOT NULL, observed_at TEXT NOT NULL,
            origin TEXT, details TEXT NOT NULL, PRIMARY KEY(message_key,revision));
        CREATE TABLE IF NOT EXISTS capture_run_state (
            run_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, project_id TEXT,
            state TEXT NOT NULL);
    ''')


def verified_payloads(root):
    from pathlib import Path
    from .backup import sha256
    cache={}
    def verify(checksum):
        import re
        if not isinstance(checksum,str) or not re.fullmatch("[0-9a-f]{64}",checksum): return False
        if checksum not in cache:
            path=Path(root)/checksum
            cache[checksum]=not path.is_symlink() and path.is_file() and sha256(path)==checksum
        return cache[checksum]
    return verify


def fingerprint(record,verified=None):
    """Unknown fields and omission evidence remain significant; no inference from visible text alone.

    Full raw/text externalization uses canonical JSON SHA256, the same representation used by capture.
    Externalized fields are comparable only when their original bytes have been verified.
    """
    from .control import category
    r=json.loads(canonical(record))
    for field in OBSERVATIONS:
        r.pop(field,None)
    attachments=[]
    external={}
    for a in r.get('attachments',[]):
        if not isinstance(a,dict):
            attachments.append(a); continue
        a=dict(a)
        a['category']=category(a)
        observation=a.pop('export_observation',{})
        field=str(a.get('kind',''))[5:]
        referenced=(field in ('raw','text') and r.get('completeness',{}).get(field+'_archived') is True and
                    (r.get('raw')=={'archived_payload':a.get('file_id')} if field=='raw' else
                     isinstance(r.get('text'),str) and r['text'].endswith('[Full text archived as '+str(a.get('file_id'))+']')))
        if (a.get('kind') in ('full_raw','full_text') and a.get('sha256')
                and referenced and observation.get('method')=='embedded_source_payload' and verified and verified(a['sha256'])):
            external[a['kind'][5:]]=(a.get('file_id'),a['sha256'])
            continue
        # Availability/scan results describe a capture; excluded/redacted content remains distinct.
        for field in ('screening',): a.pop(field,None)
        if a.get('availability') in ('captured','stored','reference_only','unavailable'):
            a.pop('availability',None)
        if observation.get('method')=='portable-context-capture':
            for field in ('sha256','size'): a.pop(field,None)
        attachments.append(a)
    r['attachments']=attachments
    completeness=r.get('completeness',{})
    for field in ('raw','text'):
        value=r.get(field,{} if field=='raw' else '')
        ref=external.get(field)
        valid=ref and completeness.get(field+'_archived') is True and (
            value=={'archived_payload':ref[0]} if field=='raw' else
            isinstance(value,str) and value.endswith('[Full text archived as '+str(ref[0])+']'))
        r[field]={'canonical_sha256':ref[1] if valid else digest(canonical(value))}
        if valid:
            completeness.pop(field+'_archived',None)
    if not completeness: r.pop('completeness',None)
    return digest(canonical({'version':VERSION,'content':r}))


def observe(con,mid,revision,record,origin=None,verified=None):
    value=fingerprint(record,verified)
    con.execute('INSERT INTO content_identity VALUES (?,?,?,?,?) ON CONFLICT(message_key,revision,version) '
                'DO UPDATE SET content_hash=excluded.content_hash,observed_at=excluded.observed_at '
                'WHERE content_identity.content_hash!=excluded.content_hash',(mid,revision,VERSION,value,now()))
    observations={k:record[k] for k in OBSERVATIONS if k in record}
    con.execute('INSERT OR IGNORE INTO export_observations VALUES (?,?,?,?,?)',
                (mid,revision,now(),origin,canonical(observations)))
    return value


def equivalent(con,mid,left,right):
    if left==right: return True
    rows=con.execute('SELECT revision,content_hash FROM content_identity WHERE message_key=? AND version=? AND revision IN (?,?)',
                     (mid,VERSION,left,right)).fetchall()
    return len(rows)==2 and rows[0]['content_hash']==rows[1]['content_hash']


def preview(store):
    """Read-only historical classification. Applying requires an exact, still-current plan."""
    groups={}; manifest=[]; affected=[]; citations=[]
    verify=verified_payloads(store.root/'originals')
    with store.connect() as con:
        for row in con.execute('SELECT id,hash,record FROM revisions ORDER BY id,hash'):
            r=json.loads(row['record']); content=fingerprint(r,verify)
            groups.setdefault((row['id'],content),[]).append(row['hash'])
            manifest.append([row['id'],row['hash'],content])
        for (mid,content),revisions in groups.items():
            if len(revisions)>1: affected.append({'message_key':mid,'content_hash':content,'revisions':revisions})
        current=[list(r) for r in con.execute('SELECT id,revision FROM messages ORDER BY id')]
        hashes={(mid,rev):content for mid,rev,content in manifest}
        heads=dict(current)
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def would_clear(ref):
            mid=ref.get('message_key'); rev=ref.get('revision'); head=heads.get(mid)
            return bool(head and head!=rev and (mid,rev) in hashes and hashes[(mid,rev)]==hashes.get((mid,head)))
        for table,field,kind in [('wiki_pages','sources','wiki_page'),('graph_edges','evidence','graph_edge')]:
            if table in tables:
                for row in con.execute('SELECT id,'+field+' FROM '+table):
                    refs=json.loads(row[1])
                    if any(would_clear(ref) for ref in refs): citations.append({'kind':kind,'id':row[0]})
    return {'version':VERSION,'exact_revisions':len(manifest),'content_versions':len(groups),
            'equivalent_groups':affected,'uncertain_or_distinct_groups':sum(len(v)==1 for v in groups.values()),
            'citations_with_equivalent_sources':citations,
            'confirmation':digest(canonical([VERSION,manifest,current,citations])),
            'effect':'Add equivalence mappings only; retain archives, current records and exact citations.'}


def apply(store,confirmation):
    from .restic_backup import guarded,protection
    @guarded
    def commit(owner):
        import uuid
        checkpoint=protection(owner.root,'identity:'+str(uuid.uuid4()),'before_migration')
        if checkpoint.get('status')=='not_configured':
            from .backup import create
            create(owner.root,owner.root.parent/(owner.root.name+'-migration-backups'))
        verify=verified_payloads(owner.root/'originals')
        with owner.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            plan=preview(owner)
            if plan['confirmation']!=confirmation: raise ValueError('Identity plan changed; review again')
            from .workspace import fingerprints
            legacy_fingerprints=fingerprints(con,semantic=False)
            for row in con.execute('SELECT id,hash,record,origin FROM revisions').fetchall():
                observe(con,row['id'],row['hash'],json.loads(row['record']),row['origin'],verify)
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage'").fetchone():
                for cid,value in fingerprints(con).items():
                    con.execute('UPDATE coverage SET fingerprint=? WHERE conversation=? AND fingerprint=?',
                                (value,cid,legacy_fingerprints[cid]))
        return {k:v for k,v in plan.items() if k!='equivalent_groups'} | {'applied':True}
    return commit(store)
