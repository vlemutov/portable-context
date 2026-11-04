"""Self-contained restic snapshots. Passwords/provider diagnostics never cross the API boundary."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import contextvars
import functools

from .core import atomic_write, canonical, digest, now
from .backup import sha256

DEFAULT_POLICY = dict(last=14, weekly=8, monthly=12, checkpoints_days=90)
_held=contextvars.ContextVar('pc_backup_write_locks',default=frozenset())

@contextmanager
def memory_lock(manager):
    name=str(manager.state)
    if name in _held.get():
        yield
    else:
        with file_lock(manager.state/'write.lock'):
            token=_held.set(_held.get()|{name})
            try: yield
            finally: _held.reset(token)

def guarded(function):
    @functools.wraps(function)
    def wrapper(owner,*args,**kwargs):
        store=getattr(owner,'store',owner)
        manager=configured(store.root)
        if manager:
            with memory_lock(manager): return function(owner,*args,**kwargs)
        return function(owner,*args,**kwargs)
    return wrapper


@contextmanager
def file_lock(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if os.name=='nt':
            import msvcrt
            if not path.stat().st_size: stream.write(b'0'); stream.flush()
            stream.seek(0)
            try: msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise ValueError('Another backup/import/restore operation is active; retry later') from None
        else:
            import fcntl
            try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('Another backup/import/restore operation is active; retry later') from None
        try: yield
        finally:
            if os.name=='nt':
                stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream,fcntl.LOCK_UN)


def configured(home):
    path=os.environ.get('PC_BACKUP_CONFIG')
    if not path:
        candidate=Path(home).resolve().parent/'restic-config.json'
        path=candidate if candidate.exists() else None
    if not path: return None
    config=json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('home') and Path(config['home']).resolve()!=Path(home).resolve(): return None
    return Manager(home,path)


def protection(home,run_id,reason='before_import'):
    manager=configured(home)
    return manager.checkpoint(reason,run_id) if manager else {'status':'not_configured'}


def validate_policy(values):
    if not isinstance(values,dict) or set(values)!=set(DEFAULT_POLICY):
        raise ValueError('Policy requires last, weekly, monthly, checkpoints_days')
    if any(type(v) is not int or not 0<=v<=10000 for v in values.values()) or values['last']<1:
        raise ValueError('Keep at least one snapshot; retention values must be integers 0..10000')
    return dict(values)


def retention(snapshots,values,protected=(),clock=None):
    """Union of newest changed snapshots, occupied calendar weeks/months and checkpoint grace."""
    values=validate_policy(values); clock=clock or datetime.now(timezone.utc)
    ordered=sorted(snapshots,key=lambda s:(s['time'],s['id']),reverse=True)
    keep=set(protected)|{s['id'] for s in ordered[:values['last']]}
    for field,count in (('weekly',values['weekly']),('monthly',values['monthly'])):
        buckets=set()
        for s in ordered:
            dt=datetime.fromisoformat(s['time'].replace('Z','+00:00'))
            bucket=dt.isocalendar()[:2] if field=='weekly' else (dt.year,dt.month)
            if bucket not in buckets and len(buckets)<count:
                buckets.add(bucket); keep.add(s['id'])
    cutoff=clock-timedelta(days=values['checkpoints_days'])
    for s in ordered:
        if s.get('checkpoint_at') and datetime.fromisoformat(s['checkpoint_at'])>=cutoff:
            keep.add(s['id'])
    return {'keep':[s['id'] for s in ordered if s['id'] in keep],
            'remove':[s['id'] for s in ordered if s['id'] not in keep]}


class Manager:
    def __init__(self,home,config):
        self.home=Path(home).resolve()
        self.config_path=Path(config).resolve()
        self.config=json.loads(self.config_path.read_text(encoding='utf-8'))
        self.state=Path(self.config['state']).resolve()
        if self.state==self.home or self.state.is_relative_to(self.home):
            raise ValueError('Backup state must be outside memory')
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,snapshot TEXT,at TEXT NOT NULL,reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pins(snapshot TEXT NOT NULL,operation TEXT NOT NULL,PRIMARY KEY(snapshot,operation));
                CREATE TABLE IF NOT EXISTS bypass(id TEXT PRIMARY KEY,actor TEXT NOT NULL,reason TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
                CREATE TRIGGER IF NOT EXISTS event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            ''')
            con.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('policy',canonical({'version':1,'values':DEFAULT_POLICY})))

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.state/'journal.sqlite3',timeout=30)) as con:
            con.row_factory=sqlite3.Row
            with con: yield con

    def event(self,kind,details):
        with self.connect() as con:
            con.execute('INSERT INTO events(at,kind,details) VALUES (?,?,?)',(now(),kind,canonical(details)))

    def run(self,args,cwd=None):
        env=dict(os.environ,RESTIC_REPOSITORY=self.config['repository'],
            RESTIC_PASSWORD_FILE=self.config['password_file'],RESTIC_CACHE_DIR=str(self.state/'cache'))
        if self.config.get('s3_config'):
            s3=json.loads(Path(self.config['s3_config']).read_text(encoding='utf-8'))
            env.update(AWS_ACCESS_KEY_ID=s3['access_key'],AWS_SECRET_ACCESS_KEY=s3['secret_key'],AWS_DEFAULT_REGION=s3['region'])
        try:
            result=subprocess.run([self.config['binary'],'--json',*args],cwd=cwd,env=env,
                capture_output=True,text=True,encoding='utf-8',timeout=3600)
        except (OSError,subprocess.TimeoutExpired):
            raise ValueError('Backup tool unavailable or timed out; credentials and tool output withheld') from None
        if result.returncode:
            self.event('error',{'operation':args[0],'exit_code':result.returncode})
            raise ValueError('Backup '+args[0]+' failed (exit '+str(result.returncode)+'); inspect connectivity, credentials and backup_status')
        return result.stdout

    def initialize(self):
        if not Path(self.config['password_file']).is_file():
            raise ValueError('Recovery password missing; do not initialize another repository')
        # The repository must be explicitly initialized; never hide a failed open by reinitializing.
        self.run(['init'])
        self.event('initialized',{'backend':'restic'})
        return {'initialized':True}

    def policy(self):
        with self.connect() as con:
            result=json.loads(con.execute("SELECT value FROM settings WHERE key='policy'").fetchone()[0])
        return {**result,'scope':'Changed snapshots; retention is the union of rules',
                'size_prediction':'Shared blocks and S3 historical versions prevent exact per-snapshot savings'}

    def bypass_once(self,run_id,actor,reason,confirmation):
        if confirmation!='IMPORT WITHOUT BACKUP' or not run_id or not actor.strip() or not reason.strip():
            raise ValueError('Explicit IMPORT WITHOUT BACKUP confirmation, run ID, actor and reason required')
        with self.connect() as con: con.execute('INSERT INTO bypass VALUES (?,?,?)',(run_id,actor,reason))
        self.event('bypass_authorized',{'run_id':run_id,'actor':actor,'reason':reason})
        return {'authorized_run':run_id,'warning':'This logical import has no recovery checkpoint'}

    def set_enabled(self,enabled,actor,confirmation=None):
        if not actor.strip() or (not enabled and confirmation!='DISABLE BACKUPS'):
            raise ValueError('Explicit DISABLE BACKUPS confirmation and actor required')
        with memory_lock(self),file_lock(self.state/'operation.lock'):
            with self.connect() as con: con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('enabled',canonical(enabled)))
            self.event('protection_changed',{'enabled':enabled,'actor':actor})
        return {'enabled':enabled,'warning':None if enabled else 'Automatic checkpoints and scheduled backups are disabled'}

    def enabled(self):
        with self.connect() as con: row=con.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
        return json.loads(row[0]) if row else True

    def snapshots(self):
        data=json.loads(self.run(['snapshots'])) or []
        with self.connect() as con:
            links={r['snapshot']:r['at'] for r in con.execute('SELECT snapshot,max(at) AS at FROM operations GROUP BY snapshot')}
        return [{**s,'checkpoint_at':links.get(s['id']) or (s['time'] if any(t.startswith('run:') for t in s.get('tags',[])) else None)} for s in data]

    def list(self,limit=20,offset=0):
        items=sorted(self.snapshots(),key=lambda s:(s['time'],s['id']),reverse=True)
        offset=max(0,offset); limit=max(1,min(limit,100))
        with self.connect() as con:
            result=[]
            for s in items[offset:offset+limit]:
                links=[dict(r) for r in con.execute('SELECT id,at,reason FROM operations WHERE snapshot=? ORDER BY at LIMIT 20',(s['id'],))]
                if not links:
                    links=[{'id':tag[4:],'at':s['time'],'reason':'snapshot_tag'} for tag in s.get('tags',[]) if tag.startswith('run:')]
                result.append({'id':s['id'],'time':s['time'],'tags':s.get('tags',[]),'operations':links,
                    'operations_count':max(len(links),con.execute('SELECT count(*) FROM operations WHERE snapshot=?',(s['id'],)).fetchone()[0]),
                    'added_bytes':s.get('summary',{}).get('data_added_packed'),
                    'logical_bytes':s.get('summary',{}).get('total_bytes_processed')})
        return {'items':result,'total':len(items),'next_offset':offset+limit if offset+limit<len(items) else None}

    def preview(self,values):
        values=validate_policy(values); snapshots=self.snapshots(); current=self.policy()
        with self.connect() as con: pins=[r[0] for r in con.execute('SELECT DISTINCT snapshot FROM pins')]
        plan=retention(snapshots,values,pins)
        payload={'values':values,'expected_version':current['version'],'snapshot_ids':sorted(s['id'] for s in snapshots),**plan}
        return {**payload,'confirmation':digest(canonical(payload)),
            'deletion':'Next scheduled cleanup, not during policy update',
            'reclaimable_bytes':None,'reason':'Deduplicated blocks and provider versions require separate accounting'}

    def set_policy(self,values,expected_version,actor,confirmation=None):
        if not actor.strip(): raise ValueError('Actor required')
        with file_lock(self.state/'operation.lock'):
            plan=self.preview(values)
            if expected_version!=plan['expected_version']: raise ValueError('Backup policy version conflict')
            if plan['remove'] and confirmation!=plan['confirmation']:
                raise ValueError('Confirm the exact preview of snapshots eligible for removal')
            with self.connect() as con:
                old=con.execute("SELECT value FROM settings WHERE key='policy'").fetchone()[0]
                new={'version':expected_version+1,'values':values}
                con.execute("UPDATE settings SET value=? WHERE key='policy'",(canonical(new),))
                con.execute('INSERT INTO events(at,kind,details) VALUES (?,?,?)',
                    (now(),'policy_changed',canonical({'actor':actor,'previous':json.loads(old),'new':new,'preview':plan})))
        return self.policy()

    def stage(self):
        stage=self.state/'stage'; stage.mkdir(exist_ok=True)
        source=self.home/'context.sqlite3'
        from .storage import tree_bytes
        required=2*source.stat().st_size+tree_bytes(self.home/'originals')+tree_bytes(self.home/'vault')
        if required>shutil.disk_usage(self.state).free*0.8:
            raise ValueError('Insufficient free disk space for a consistent backup with 20% reserve; inspect storage_report')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(stage/'context.sqlite3')) as dst:
            src.backup(dst,pages=256)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Snapshot database is corrupt')
            workspace=dst.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
            originals=dict(dst.execute('SELECT hash,size FROM imports'))
            counts={t:dst.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('messages','revisions','imports')}
            # Business content changes, not transport attempts or backup bookkeeping.
            tables={r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            h=hashlib.sha256()
            for table in ('messages','revisions','imports','catalog_projects','catalog_assignments','catalog_annotations',
                          'project_groups','capture_policies','source_sessions','wiki_pages','wiki_history','note_history',
                          'project_roots','project_file_current','project_file_versions','index_policy','metadata'):
                if table not in tables: continue
                h.update(table.encode())
                for row in dst.execute('SELECT * FROM '+table+' ORDER BY rowid'):
                    h.update(canonical(row).encode('utf-8'))
            for table in ('graph_nodes','graph_edges'):
                if table not in tables: continue
                columns=[r[1] for r in dst.execute('PRAGMA table_info('+table+')') if r[1]!='updated_at']
                h.update(table.encode())
                for row in dst.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY id'):
                    h.update(canonical(row).encode('utf-8'))
        original_dir=stage/'originals'; original_dir.mkdir(exist_ok=True)
        for checksum,size in originals.items():
            if not re.fullmatch('[0-9a-f]{64}',checksum): raise ValueError('Invalid original hash')
            src=self.home/'originals'/checksum
            if src.is_symlink() or not src.is_file() or src.stat().st_size!=size or sha256(src)!=checksum:
                raise ValueError('Original missing or corrupt; backup refused')
            target=original_dir/checksum
            if not target.exists() or target.stat().st_size!=size or sha256(target)!=checksum:
                shutil.copyfile(src,target)
        for path in original_dir.iterdir():
            if path.name not in originals: path.unlink()
        vault=stage/'recovered-vault'; vault.mkdir(exist_ok=True)
        present=set()
        for path in sorted((self.home/'vault').rglob('*')):
            if path.is_symlink(): raise ValueError('Vault symlinks cannot be backed up')
            if not path.is_file() or path.name.startswith('.pc-'): continue
            rel=path.relative_to(self.home/'vault'); present.add(rel)
            target=vault/rel; target.parent.mkdir(parents=True,exist_ok=True)
            data=path.read_bytes(); h.update(rel.as_posix().encode()); h.update(hashlib.sha256(data).digest())
            if not target.exists() or target.read_bytes()!=data: target.write_bytes(data)
        for path in vault.rglob('*'):
            if path.is_file() and path.relative_to(vault) not in present: path.unlink()
        config={'backup_policy':self.policy()['values']}
        h.update(canonical(config).encode())
        atomic_write(stage/'memory-config.json',canonical(config))
        fingerprint=h.hexdigest()
        # SQLite scatters changed pages across a large file. Fixed page-aligned chunks avoid
        # rewriting multi-MiB content-defined restic blocks for a tiny transaction.
        pages=stage/'database-pages'; pages.mkdir(exist_ok=True)
        page_names=[]
        with (stage/'context.sqlite3').open('rb') as stream:
            index=0
            while data:=stream.read(16384):
                name=f'{index:08d}.bin'; page_names.append(name); target=pages/name
                if not target.exists() or target.read_bytes()!=data: target.write_bytes(data)
                index+=1
        page_set=set(page_names)
        for p in pages.iterdir():
            if p.name not in page_set: p.unlink()
        manifest={'format':'portable-context-restic-1','workspace_id':workspace,'counts':counts,
            'originals':originals,'fingerprint':fingerprint,'database_sha256':sha256(stage/'context.sqlite3'),
            'database_parts':['database-pages/'+name for name in page_names],
            'files':{p.relative_to(stage).as_posix():sha256(p) for p in stage.rglob('*')
                if p.is_file() and p.name not in ('manifest.json','context.sqlite3')}}
        atomic_write(stage/'manifest.json',canonical(manifest))
        return stage,manifest

    def checkpoint(self,reason='scheduled',run_id=None):
        with memory_lock(self),file_lock(self.state/'operation.lock'):
            if not self.enabled():
                self.event('protection_disabled',{'reason':reason,'run_id':run_id})
                return {'status':'disabled','run_id':run_id}
            if run_id:
                with self.connect() as con:
                    prior=con.execute('SELECT * FROM operations WHERE id=?',(run_id,)).fetchone()
                    bypass=con.execute('SELECT * FROM bypass WHERE id=?',(run_id,)).fetchone()
                if bypass:
                    self.event('bypass_used',{'run_id':run_id,'actor':bypass['actor']})
                    return {'status':'explicit_bypass','run_id':run_id}
                if prior:
                    return {'status':'reused','snapshot_id':prior['snapshot'],'run_id':run_id}
            try:
                stage,manifest=self.stage()
                snapshots=self.snapshots()
                existing=next((s for s in snapshots if 'content:'+manifest['fingerprint'] in s.get('tags',[])),None)
                if existing:
                    sid=existing['id']; status='skipped'; added=0
                else:
                    output=self.run(['backup','database-pages','originals','recovered-vault','manifest.json','memory-config.json',
                        '--host',manifest['workspace_id'],'--tag','content:'+manifest['fingerprint'],
                        '--tag',reason]+(['--tag','run:'+run_id] if run_id else []),cwd=stage)
                    records=[json.loads(line) for line in output.splitlines() if line.strip()]
                    result=next(r for r in records if r.get('message_type')=='summary')
                    sid=result['snapshot_id']; status='created'; added=result.get('data_added_packed')
                if run_id:
                    with self.connect() as con:
                        con.execute('INSERT INTO operations VALUES (?,?,?,?)',(run_id,sid,now(),reason))
                result={'status':status,'snapshot_id':sid,'run_id':run_id,'reason':reason,'added_bytes':added,
                        'fingerprint':manifest['fingerprint'],'workspace_id':manifest['workspace_id']}
                self.event('snapshot' if status=='created' else 'skipped',result)
                return result
            except Exception:
                self.event('checkpoint_failed',{'reason':reason,'run_id':run_id})
                raise

    def restore(self,snapshot,target,verify_sample=None):
        if not re.fullmatch('[0-9a-f]{64}',snapshot): raise ValueError('Use a full snapshot ID from list_backups')
        target=Path(target).resolve()
        if target.exists(): raise ValueError('Restore requires a new directory')
        if target==self.home or target.is_relative_to(self.home) or target.is_relative_to(self.state):
            raise ValueError('Restore target must be outside live memory and backup state')
        operation=str(uuid.uuid4())
        with file_lock(self.state/'operation.lock'):
            with self.connect() as con: con.execute('INSERT INTO pins VALUES (?,?)',(snapshot,operation))
            try:
                target.parent.mkdir(parents=True,exist_ok=True)
                candidates=[s for s in self.snapshots() if s['id']==snapshot]
                if not candidates: raise ValueError('Snapshot no longer exists')
                logical=candidates[0].get('summary',{}).get('total_bytes_processed',0)
                if 3*logical>shutil.disk_usage(target.parent).free*0.8:
                    raise ValueError('Insufficient disk space to restore and reconstruct this snapshot')
                self.run(['restore',snapshot,'--target',str(target)])
                manifest=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
                if manifest['format']!='portable-context-restic-1': raise ValueError('Unsupported backup format')
                if manifest.get('database_parts'):
                    parts=manifest['database_parts']
                    if parts!=[f'database-pages/{i:08d}.bin' for i in range(len(parts))]: raise ValueError('Invalid database part order')
                    with (target/'context.sqlite3').open('wb') as output:
                        for rel in manifest['database_parts']:
                            part=(target/rel).resolve()
                            if not part.is_relative_to(target/'database-pages') or not part.is_file():
                                raise ValueError('Invalid database page path')
                            output.write(part.read_bytes())
                if sha256(target/'context.sqlite3')!=manifest['database_sha256']:
                    raise ValueError('Restored database checksum mismatch')
                # Restic authenticates every restored blob; additionally verify our database and originals.
                files=manifest['files']
                checks=list(files) if verify_sample is None else random.sample(list(files),min(verify_sample,len(files)))
                for rel in checks:
                    p=(target/rel).resolve()
                    if not p.is_relative_to(target) or not p.is_file() or sha256(p)!=files[rel]:
                        raise ValueError('Restored checksum mismatch')
                with closing(sqlite3.connect(target/'context.sqlite3')) as con:
                    if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Restored database corrupt')
                    wid=con.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
                    if wid!=manifest['workspace_id']: raise ValueError('Restored workspace mismatch')
                    for table,count in manifest['counts'].items():
                        if table not in ('messages','revisions','imports') or con.execute('SELECT count(*) FROM '+table).fetchone()[0]!=count:
                            raise ValueError('Restored count mismatch')
                # Preserve original manual files; generated views can be rebuilt after activation.
                shutil.copytree(target/'recovered-vault',target/'vault',dirs_exist_ok=True)
                result={'snapshot_id':snapshot,'target':str(target),'workspace_id':wid,'verified':True,
                    'verified_files':len(checks),'manifest_sha256':sha256(target/'manifest.json'),
                    'activation':'Separate explicit owner confirmation required'}
                atomic_write(target/'restore-receipt.json',canonical(result))
                self.event('restore_verified',result)
                return result
            except Exception:
                self.event('restore_failed',{'snapshot_id':snapshot,'operation':operation})
                raise
            finally:
                with self.connect() as con: con.execute('DELETE FROM pins WHERE operation=?',(operation,))

    def check(self):
        snapshots=self.snapshots()
        if not snapshots: raise ValueError('No snapshots to verify')
        chosen=random.choice(snapshots)['id']
        with tempfile.TemporaryDirectory(prefix='pc-restore-check-',dir=self.home.parent) as tmp:
            report=self.restore(chosen,Path(tmp)/'memory',verify_sample=20)
        with file_lock(self.state/'operation.lock'):
            self.run(['check','--read-data-subset=5%'])
        self.event('check_verified',{'snapshot_id':chosen,'verified_files':report['verified_files'],'restic_subset':'5%'})
        return {'verified':True,'snapshot_id':chosen}

    def prune(self):
        with file_lock(self.state/'operation.lock'):
            if not self.enabled(): return {'status':'disabled','removed_snapshots':[]}
            plan=self.preview(self.policy()['values'])
            if plan['remove']:
                self.run(['forget',*plan['remove']])
            self.run(['prune'])  # Also finishes an interrupted prune after successful forget.
            self.run(['check'])
            result={'removed_snapshots':plan['remove'],'kept':len(plan['keep']),
                    's3_versions':'Provider historical versions are separate; no bucket lifecycle changes applied'}
            self.event('retention_applied',result)
            return result

    def status(self):
        with self.connect() as con:
            latest={}
            for kind in ('snapshot','skipped','check_verified','restore_verified','error','checkpoint_failed','restore_failed','policy_changed','retention_applied','bypass_used','protection_changed'):
                r=con.execute('SELECT at,details FROM events WHERE kind=? ORDER BY seq DESC LIMIT 1',(kind,)).fetchone()
                latest[kind]={'at':r['at'],**json.loads(r['details'])} if r else None
        return {'configured':True,'enabled':self.enabled(),'backend':'restic','policy':self.policy(),'latest':latest,
                'originals_included':True,'secret_values':'never exposed'}


def main(argv=None):
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',required=True); p.add_argument('--config',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('init','backup','status','list','check','prune'): sub.add_parser(name)
    r=sub.add_parser('restore'); r.add_argument('--snapshot',required=True); r.add_argument('--target',required=True)
    r=sub.add_parser('bypass-once'); r.add_argument('--run-id',required=True); r.add_argument('--actor',required=True); r.add_argument('--reason',required=True); r.add_argument('--confirm',required=True)
    for name in ('enable','disable'):
        r=sub.add_parser(name); r.add_argument('--actor',required=True); r.add_argument('--confirm')
    args=p.parse_args(argv); manager=Manager(args.home,args.config)
    actions={'init':manager.initialize,'backup':manager.checkpoint,'status':manager.status,'list':manager.list,
             'check':manager.check,'prune':manager.prune}
    if args.command=='restore': result=manager.restore(args.snapshot,args.target)
    elif args.command=='bypass-once': result=manager.bypass_once(args.run_id,args.actor,args.reason,args.confirm)
    elif args.command in ('enable','disable'): result=manager.set_enabled(args.command=='enable',args.actor,args.confirm)
    else: result=actions[args.command]()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
