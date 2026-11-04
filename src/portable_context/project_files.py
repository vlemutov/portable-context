"""Project-owned file evidence: explicit folder binding, no fabricated conversation messages."""
import fnmatch,hashlib,json,os,re,shutil,tempfile,uuid
from pathlib import Path,PurePosixPath
from .core import canonical,digest,key,now,atomic_write
from .policy import Policy,agent_workspace,permissions
from .privacy import screen_bytes

EXCLUDED_DIRS={'.git','.hg','.svn','node_modules','.venv','venv','__pycache__','.cache','.pytest_cache',
    '.mypy_cache','.next','dist','build','target','vendor'}
SECRET_NAMES={'.env','credentials.json','credentials','id_rsa','id_ed25519','backup-config.json','restic-password'}
LARGE_SUFFIXES={'.gguf','.safetensors','.ckpt','.pt','.pth','.onnx','.parquet','.arrow'}

def initialize(con):
    for sql in (
        'CREATE TABLE IF NOT EXISTS project_roots(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,instance TEXT NOT NULL,description TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS project_file_versions(occurrence_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,binding_id TEXT NOT NULL,path TEXT NOT NULL,snapshot TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS project_file_current(binding_id TEXT NOT NULL,path TEXT NOT NULL,occurrence_id TEXT NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(binding_id,path))',
        'CREATE TABLE IF NOT EXISTS project_snapshots(id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,applied_at TEXT NOT NULL)',
        '''CREATE VIEW IF NOT EXISTS catalog_owned_occurrences AS SELECT o.*,coalesce(p.project_id,a.project_id) AS owner_project,
            CASE WHEN p.occurrence_id IS NOT NULL THEN coalesce(c.occurrence_id=o.id AND c.present=1,0)
            ELSE coalesce(m.revision=o.revision,0) END AS current_source
            FROM catalog_occurrences o LEFT JOIN messages m ON m.id=o.message_key
            LEFT JOIN catalog_assignments a ON a.conversation=o.conversation
            LEFT JOIN project_file_versions p ON p.occurrence_id=o.id
            LEFT JOIN project_file_current c ON c.binding_id=p.binding_id AND c.path=p.path'''):
        con.execute(sql)
    columns={r[1] for r in con.execute('PRAGMA table_info(project_snapshots)')}
    if 'sequence' not in columns: con.execute('ALTER TABLE project_snapshots ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0')

def selection(root,project_id,policy=None,large=False,excludes=()):
    root=Path(root).expanduser().resolve(); policy=policy or Policy.load(None)
    if not root.is_dir() or root==Path(root.anchor) or root==Path.home().resolve():
        raise ValueError('Select a concrete project folder, not a disk or home directory')
    entries=[]
    def scan_error(error): raise error
    for directory,dirs,files in os.walk(root,followlinks=False,onerror=scan_error):
        here=Path(directory)
        kept=[]
        for name in sorted(dirs):
            p=here/name; rel=p.relative_to(root).as_posix()
            reason='symlink' if p.is_symlink() else 'dependency_or_cache' if name in EXCLUDED_DIRS else None
            if any(fnmatch.fnmatchcase(rel+'/',pattern) or fnmatch.fnmatchcase(rel,pattern) for pattern in excludes): reason='user_exclusion'
            if reason: entries.append({'path':rel+'/','reason':reason,'bytes':0,'directory':True})
            else: kept.append(name)
        dirs[:]=kept
        for name in sorted(files):
            p=here/name; rel=p.relative_to(root).as_posix()
            reason=None
            if p.is_symlink() or not p.resolve().is_relative_to(root): reason='symlink_or_outside_root'
            elif name in SECRET_NAMES or name.startswith('.env.') or p.suffix.lower() in ('.key','.pem','.p12','.pfx'): reason='secret_filename'
            elif agent_workspace(p) and not ('memory' in p.parts and '.claude' in p.parts and p.suffix.lower()=='.md'): reason='agent_runtime'
            elif any(fnmatch.fnmatchcase(rel,pattern) for pattern in excludes): reason='user_exclusion'
            record={'source':'project-files','instance':'local','conversation_id':'folder','raw':{'cwd':str(p.parent)},'memory_project':{'id':project_id}}
            allowed,_=policy.select(record,project_id)
            if not allowed['conversation']:
                # Retain coverage uncertainty without disclosing a denied filename.
                if not any(x.get('reason')=='private_scope_unobserved' for x in entries):
                    entries.append({'path':'','reason':'private_scope_unobserved','directory':True,'bytes':0})
                continue
            if not allowed['document_bytes']: reason='capture_policy'
            size=p.stat().st_size if not p.is_symlink() and p.is_file() else 0
            if size>512*1024*1024: reason='exceeds_512_mib'
            elif not large and (size>50*1024*1024 or p.suffix.lower() in LARGE_SUFFIXES): reason=reason or 'large_file_needs_selection'
            item={'path':rel,'bytes':size,'reason':reason}
            if not reason:
                before=p.stat(); data=p.read_bytes(); after=p.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('Project file changed during scan; retry')
                found,method=screen_bytes(data)
                if found: item['reason']='secret_detected'
                else: item.update(sha256=hashlib.sha256(data).hexdigest(),screening=method)
            entries.append(item)
            if len(entries)>20000: raise ValueError('Project exceeds 20000 observations; select narrower folders')
    return entries

def preview(root,project_id,workspace_id,policy=None,large=False,excludes=()):
    entries=selection(root,project_id,policy,large,excludes)
    data={'root':str(Path(root).expanduser().resolve()),'project_id':project_id,'workspace_id':workspace_id,
        'include_large':large,'excludes':list(excludes),'files':entries}
    return {**data,'plan_id':digest(canonical(data)),'selected_bytes':sum(x['bytes'] for x in entries if not x['reason']),
        'selected_files':sum(not x['reason'] for x in entries),'excluded_files':sum(bool(x['reason']) for x in entries),
        'note':'Policy/secret exclusions win. Bind this exact root once; background collection is a separate opt-in.'}

def bind(profile,root,project_id,approval,large=False,excludes=(),policy=None):
    profile=Path(profile); connection=json.loads(profile.read_text(encoding='utf-8'))
    plan=preview(root,project_id,connection['workspace_id'],policy,large,excludes)
    if approval!=plan['plan_id']: raise ValueError('Folder plan changed; review and approve the new plan ID')
    device=profile.parent/'project-device.json'
    if not device.exists(): atomic_write(device,canonical({'id':str(uuid.uuid4())}))
    bid=key('project-binding',json.loads(device.read_text(encoding='utf-8'))['id'],connection['workspace_id'],project_id,plan['root'])
    data={k:plan[k] for k in ('root','project_id','workspace_id','include_large','excludes','selected_bytes')}
    binding_path=profile.parent/'project-bindings'/(bid+'.json')
    old=json.loads(binding_path.read_text(encoding='utf-8')) if binding_path.exists() else {}
    data.update(id=bid,basis='user_selection',instance=bid,approved_at=now(),sequence=old.get('sequence',0))
    atomic_write(binding_path,canonical(data))
    return data

def prepare(profile,binding_id,destination,policy=None,accept_growth=False,agent=None):
    from .restic_backup import file_lock
    with file_lock(Path(profile).parent/'project-bindings'/'.prepare.lock'):
        return _prepare(profile,binding_id,destination,policy,accept_growth,agent)

def _prepare(profile,binding_id,destination,policy=None,accept_growth=False,agent=None):
    from . import __version__
    profile=Path(profile); connection=json.loads(profile.read_text(encoding='utf-8'))
    if not re.fullmatch('[0-9a-f]{64}',binding_id): raise ValueError('Invalid binding ID')
    binding=json.loads((profile.parent/'project-bindings'/(binding_id+'.json')).read_text(encoding='utf-8'))
    if binding['workspace_id']!=connection['workspace_id']: raise ValueError('Binding belongs to another memory')
    entries=selection(binding['root'],binding['project_id'],policy,binding['include_large'],binding['excludes'])
    size=sum(x['bytes'] for x in entries if not x['reason'])
    if size>max(100*1024**2,binding['selected_bytes']*2) and not accept_growth:
        raise ValueError('Project grew substantially; review folder preview and use --accept-growth after owner approval')
    destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('Use a new capture destination')
    if destination.is_relative_to(Path(binding['root'])): raise ValueError('Capture destination must be outside project root')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if size>shutil.disk_usage(destination.parent).free*0.8: raise ValueError('Insufficient free space for project capture with 20% reserve')
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        folder=Path(tmp)/'package'; (folder/'blobs').mkdir(parents=True)
        files=[]
        for item in entries:
            if item.get('directory'): continue
            a={'relative_path':item['path'],'name':Path(item['path']).name,'role':'unknown','purpose':'unknown',
                'status':'unknown','category':'document','basis':'user_selected_project_folder','size':item['bytes'],
                'source_root':binding['root'],'binding_id':binding_id}
            if item['reason']: a.update(availability='excluded',reason=item['reason'])
            else:
                data=(Path(binding['root'])/item['path']).read_bytes()
                if hashlib.sha256(data).hexdigest()!=item['sha256']: raise ValueError('File changed after scan; retry')
                (folder/'blobs'/item['sha256']).write_bytes(data)
                a.update(sha256=item['sha256'],availability='captured',screening=item['screening'])
            files.append(a)
        snapshot={'binding_id':binding_id,'project_id':binding['project_id'],'workspace_id':binding['workspace_id'],
            'instance':binding['instance'],'root':binding['root'],'basis':'user_selection','files':files}
        snapshot['coverage_complete']=not any(x['reason']=='private_scope_unobserved' for x in entries)
        snapshot['unobserved_prefixes']=[x['path'] for x in entries if x.get('directory') and x['path']]
        binding['sequence']=binding.get('sequence',0)+1
        snapshot['sequence']=binding['sequence']
        # Reserve sequence before publishing: interrupted captures may leave harmless gaps.
        atomic_write(profile.parent/'project-bindings'/(binding_id+'.json'),canonical(binding))
        snapshot['captured_at']=now()
        snapshot['snapshot_id']=digest(canonical(snapshot))
        data=b'[]'; (folder/'records.json').write_bytes(data)
        manifest={'format':'portable-context-capture-2','created_at':now(),'messages':0,'project_snapshot':snapshot,
            'parts':[{'path':'records.json','sha256':hashlib.sha256(data).hexdigest(),'size':2,'messages':0}],
            'blobs':[{'path':'blobs/'+p.name,'sha256':p.name,'size':p.stat().st_size} for p in sorted((folder/'blobs').iterdir())],
            'capture_scopes':[],'executor':{'client':'portable-context','version':__version__,'agent':agent},
            'unique_bytes':sum(p.stat().st_size for p in (folder/'blobs').iterdir()),
            'missing_occurrences':0,'excluded_occurrences':sum(bool(x['reason']) for x in entries)}
        if len(canonical(manifest).encode())>8*1024**2: raise ValueError('Project manifest exceeds 8 MiB; select narrower root')
        atomic_write(folder/'manifest.json',canonical(manifest))
        # Human-readable entry and passports exist at export time, before the source disappears.
        atomic_write(folder/'catalog/project.md','# Selected project folder\n\n'+binding['root']+'\n\n'+
            '\n'.join('- '+a['relative_path']+' — '+a['availability'] for a in files))
        for a in files: atomic_write(folder/'catalog/files'/(digest(a['relative_path'])+'.md'),canonical(a))
        folder.rename(destination)
    return {'directory':str(destination),'binding_id':binding_id,'files':len(files),'unique_bytes':manifest['unique_bytes'],
        'excluded':manifest['excluded_occurrences'],'snapshot_id':snapshot['snapshot_id']}

def validate_snapshot(con,snapshot,workspace_id=None):
    if snapshot.get('basis')!='user_selection': raise ValueError('Explicit project folder selection required')
    for field in ('binding_id','project_id','snapshot_id'):
        if not re.fullmatch('[0-9a-f]{64}',snapshot.get(field,'')): raise ValueError('Invalid project snapshot identifier')
    if workspace_id and snapshot.get('workspace_id')!=workspace_id: raise ValueError('Project capture belongs to another memory')
    if type(snapshot.get('sequence')) is not int or snapshot['sequence']<1: raise ValueError('Positive folder snapshot sequence required')
    if not con.execute('SELECT 1 FROM catalog_projects WHERE id=?',(snapshot['project_id'],)).fetchone(): raise ValueError('Unknown target project')
    p=permissions(con,snapshot['project_id'])
    if not p['conversation']: raise ValueError('Capture policy excludes this project')
    seen=set()
    for a in snapshot['files']:
        rel=a['relative_path']; path=PurePosixPath(rel)
        if not rel or path.is_absolute() or '..' in path.parts or '\\' in rel or ':' in rel or rel in seen:
            raise ValueError('Invalid or duplicate project relative path')
        seen.add(rel)
        if a.get('sha256') and (not re.fullmatch('[0-9a-f]{64}',a['sha256']) or not p['document_bytes']):
            raise ValueError('Capture policy or hash rejects project bytes')
    plain={k:v for k,v in snapshot.items() if k!='snapshot_id'}
    if digest(canonical(plain))!=snapshot['snapshot_id']: raise ValueError('Project snapshot checksum mismatch')

def import_snapshot(store,directory,manifest,run_id,transfer_limit=None):
    from .catalog import Catalog,project_catalog,_audit
    from .import_log import start,progress
    from .restic_backup import protection
    snap=manifest['project_snapshot']; directory=Path(directory); package_id=digest(canonical(manifest))
    with store.connect() as con:
        wid=con.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
        validate_snapshot(con,snap,wid)
    for a in snap['files']:
        if a.get('sha256'):
            path=directory/'blobs'/a['sha256']
            from .backup import sha256
            if not path.is_file() or sha256(path)!=a['sha256']: raise ValueError('Project blob missing or corrupt')
    run_id=run_id or str(uuid.uuid4()); protection(store.root,run_id)
    observed=set(); transfers=[]; counts={'inserted':0,'revised':0,'unchanged':0}
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE'); validate_snapshot(con,snap,wid)
        start(con,package_id,manifest.get('executor'),run_id,kind='project_folder')
        old=con.execute('SELECT project_id FROM project_roots WHERE id=?',(snap['binding_id'],)).fetchone()
        if old and old[0]!=snap['project_id']: raise ValueError('Folder binding project changed')
        con.execute('INSERT OR IGNORE INTO project_roots VALUES (?,?,?,?)',
            (snap['binding_id'],snap['project_id'],snap['instance'],canonical({'root':snap['root'],'basis':snap['basis']})))
        replay=bool(con.execute('SELECT 1 FROM project_snapshots WHERE id=?',(snap['snapshot_id'],)).fetchone())
        latest=con.execute('SELECT coalesce(max(sequence),0) FROM project_snapshots WHERE binding_id=?',(snap['binding_id'],)).fetchone()[0]
        apply_current=not replay and snap['sequence']>latest
        for a in snap['files']:
            rel=a['relative_path']; observed.add(rel)
            fid=key('project-file',snap['project_id'],snap['binding_id'],rel)
            revision=digest(canonical(a)); oid=key('project-occurrence',fid,revision)
            current=con.execute('SELECT occurrence_id,present FROM project_file_current WHERE binding_id=? AND path=?',(snap['binding_id'],rel)).fetchone()
            counts['unchanged' if current and current[0]==oid and current[1] else 'revised' if current else 'inserted']+=1
            con.execute('INSERT OR IGNORE INTO catalog_files VALUES (?,?,?)',(fid,a['name'],now()))
            con.execute('''INSERT OR IGNORE INTO catalog_occurrences
                (id,file_id,conversation,message_key,revision,position,metadata,sha256,availability,observed_at,category)
                VALUES (?,?,'','',?,0,?,NULL,?,?,'document')''',
                (oid,fid,revision,canonical(a),'excluded' if a['availability']=='excluded' else 'reference_only',now()))
            con.execute('INSERT OR IGNORE INTO project_file_versions VALUES (?,?,?,?,?)',(oid,snap['project_id'],snap['binding_id'],rel,snap['snapshot_id']))
            if apply_current: con.execute('INSERT OR REPLACE INTO project_file_current VALUES (?,?,?,1)',(snap['binding_id'],rel,oid))
            from .graph import node,edge
            node(con,'file:'+fid,'file',a['name'],{'file_id':fid,'basis':'project_folder_selection'})
            node(con,'memory-project:'+snap['project_id'],'memory_project',
                 con.execute('SELECT title FROM catalog_projects WHERE id=?',(snap['project_id'],)).fetchone()[0],{'project_id':snap['project_id']})
            if apply_current: edge(con,'file:'+fid,'in_memory_project','memory-project:'+snap['project_id'],'structural',
                 'project-file:'+fid,[{'file_occurrence':oid,'revision':revision}],'user_selection')
            if a.get('sha256'): transfers.append((oid,a['sha256']))
        # Absence is an observation, not destruction. Replaying an older snapshot must not regress current state.
        for row in con.execute('SELECT path,occurrence_id FROM project_file_current WHERE binding_id=? AND present=1',(snap['binding_id'],)).fetchall():
            covered=snap.get('coverage_complete',False) and not any(row['path'].startswith(p) for p in snap.get('unobserved_prefixes',[]))
            if apply_current and covered and row['path'] not in observed:
                con.execute('UPDATE project_file_current SET present=0 WHERE binding_id=? AND path=?',(snap['binding_id'],row['path']))
                _audit(con,'project-capture','project_file_absent',{'binding_id':snap['binding_id'],'path':row['path'],'snapshot_id':snap['snapshot_id']})
        con.execute('INSERT OR IGNORE INTO project_snapshots(id,binding_id,applied_at,sequence) VALUES (?,?,?,?)',(snap['snapshot_id'],snap['binding_id'],now(),snap['sequence']))
        progress(con,run_id,dict(inserted=0,revised=0,unchanged=0),[],batch_key=snap['snapshot_id'])
        details=json.loads(con.execute('SELECT details FROM import_runs WHERE id=?',(run_id,)).fetchone()[0])
        if 'project_files' not in details: details.update(project_files=counts,projects=[snap['project_id']])
        con.execute('UPDATE import_runs SET details=? WHERE id=?',(canonical(details),run_id))
    catalog=Catalog(store,[directory]); receipts=[]; pending=[]
    for oid,sha in transfers:
        o=catalog.occurrence(oid)
        with store.connect() as con: blob=con.execute('SELECT s3_verified_at FROM catalog_blobs WHERE sha256=?',(sha,)).fetchone()
        if o['availability']=='stored' and (not os.environ.get('PC_ATTACHMENT_S3_CONFIG') or blob and blob[0]):
            receipts.append({'occurrence_id':oid,'s3':{'status':'verified' if os.environ.get('PC_ATTACHMENT_S3_CONFIG') else 'not_configured'}})
        else: pending.append((oid,sha))
    for oid,sha in pending[:transfer_limit] if transfer_limit else pending:
        receipts.append(catalog.store_attachment(oid,server_path=str(directory/'blobs'/sha),expected_sha256=sha,project=False))
    complete=len(receipts)==len(transfers) and all(r['s3']['status']!='failed' for r in receipts)
    result={'package_id':package_id,'run_id':run_id,'messages':dict(inserted=0,revised=0,unchanged=0),
        'project_files':counts,'attachments':receipts,'complete':complete,'remaining_transfers':len(transfers)-len(receipts),
        'project_id':snap['project_id'],'completeness':{'excluded_occurrences':manifest.get('excluded_occurrences',0)}}
    with store.connect() as con:
        progress(con,run_id,result['messages'],[],complete=complete,receipt=result)
        con.execute('INSERT OR REPLACE INTO capture_receipts VALUES (?,?,?)',(package_id,now(),canonical(result)))
    if complete: project_catalog(store)
    return result
