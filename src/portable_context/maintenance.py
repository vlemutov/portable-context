"""Owner-operated Linux maintenance. Destructive operations require exact preview receipts."""
import json,os,re,shutil,sqlite3,uuid
from pathlib import Path
from contextlib import closing
from .core import canonical,digest,atomic_write,now
from .restic_backup import configured,file_lock,memory_lock
from .storage import tree_bytes

def restic_verified(manager):
    if not manager: return False
    with manager.connect() as con:
        return any(re.fullmatch('[0-9a-f]{64}',json.loads(r[0]).get('snapshot_id',''))
            for r in con.execute("SELECT details FROM events WHERE kind IN ('restore_verified','check_verified')"))

def cleanup_preview(root):
    root=Path(root).resolve(); memory=root/'memory'; candidates=[]; kept=[]
    current=(root/'current').resolve() if (root/'current').exists() else None
    previous=(root/'previous').resolve() if (root/'previous').exists() else None
    releases=sorted((root/'releases').glob('*'),key=lambda p:p.stat().st_mtime,reverse=True)
    protected={current,previous}
    if previous is None: protected.update(p.resolve() for p in releases[:2])
    def item(path,kind,reason):
        return {'path':str(path.absolute()),'kind':kind,'bytes':tree_bytes(path) if path.is_dir() else path.stat().st_size,
                'timestamp_ns':path.stat().st_mtime_ns,'timestamp_basis':'filesystem_mtime', 'reason':reason}
    for path in releases:
        if path.is_symlink():
            kept.append(item(path,'old_release','symlink_not_managed')); continue
        if path.is_dir():
            (kept if path.resolve() in protected else candidates).append(item(path,'old_release',
                'active_or_rollback_release' if path.resolve() in protected else 'outside_release_retention'))
    from .recovery_coverage import verified,inventory,package_requirements,covers
    manager=configured(memory)
    try: snapshots=verified(manager)
    except (ValueError,OSError): snapshots=[]
    roots=[root/'imports',root/'imports/uploads']
    if (memory/'context.sqlite3').exists():
        with closing(sqlite3.connect((memory/'context.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
            available=inventory(con)
            for managed in roots:
                if managed.is_symlink(): continue
                for path in sorted(managed.glob('*')):
                    if path==root/'imports/uploads': continue
                    if not path.is_dir(): continue
                    entry=item(path,'completed_upload','unknown_or_incomplete_package')
                    if path.is_symlink():
                        entry['reason']='symlink_not_managed'; kept.append(entry); continue
                    try:
                        manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
                        pid=digest(canonical(manifest))
                        receipt=con.execute('SELECT report FROM capture_receipts WHERE id=?',(pid,)).fetchone()
                        active=con.execute("SELECT 1 FROM import_runs WHERE package_id=? AND status!='complete' LIMIT 1",(pid,)).fetchone()
                        if not receipt or not json.loads(receipt[0]).get('complete') or active:
                            entry['reason']='incomplete_or_active_import'
                        else:
                            required=package_requirements(path,con)
                            covering=next((s for s in snapshots if covers(s,required)),None)
                            if not covers(available,required): entry['reason']='contents_not_proven_in_memory'
                            elif not covering:
                                entry.update(reason='no_verified_snapshot_covering_contents',next='Run restic post-import, then preview again')
                            else:
                                entry.update(reason='contents_in_memory_and_verified_snapshot',snapshot_id=covering['snapshot_id'],
                                             package_id=pid,manifest_sha256=digest(canonical(manifest)))
                                candidates.append(entry); continue
                    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                        entry['reason']='unrecognized_or_unverifiable_package'
                    kept.append(entry)
    for folder in (root/'backups',root/'memory-migration-backups'):
        for path in sorted(folder.glob('*.zip')):
            kept.append(item(path,'legacy_zip','archive_recovery_coverage_not_proven'))
    plan={'root':str(root),'items':sorted(candidates,key=lambda x:x['path']),
          'kept':sorted(kept,key=lambda x:x['path']),'bytes':sum(c['bytes'] for c in candidates)}
    return {**plan,'confirmation':digest(canonical(plan)),'scope':'Local managed files only; S3 objects untouched'}


def cleanup(root,confirmation):
    root=Path(root).resolve()
    with file_lock(root/'install.lock'):
        manager=configured(root/'memory')
        lock=manager.state/'write.lock' if manager else root/'cleanup.lock'
        with file_lock(lock):
            plan=cleanup_preview(root)
            if confirmation!=plan['confirmation']: raise ValueError('Cleanup preview changed; review and confirm the exact current plan')
            for item in plan['items']:
                original=Path(item['path'])
                if original.is_symlink() or original.parent.is_symlink(): raise ValueError('Cleanup symlink refused')
                path=original.resolve()
                allowed={'old_release':root/'releases','completed_upload':path.parent if path.parent in (root/'imports',root/'imports/uploads') else root/'imports/uploads',
                    'legacy_zip':root/'backups' if path.parent==root/'backups' else root/'memory-migration-backups'}[item['kind']]
                if path.parent!=allowed or path.is_symlink(): raise ValueError('Cleanup path escaped its approved directory')
                if path.is_dir():
                    shutil.rmtree(path)
                else: path.unlink()
            if manager: manager.event('cleanup_applied',{'items':plan['items'],'bytes':plan['bytes']})
            return {'removed':plan['items'],'bytes':plan['bytes']}


def s3_cleanup_preview(root):
    root=Path(root).resolve(); manager=configured(root/'memory')
    if not restic_verified(manager): raise ValueError('Verify a restic restoration before cleaning legacy S3 backups')
    from .backup import s3_client
    config=json.loads((root/'backup-config.json').read_text(encoding='utf-8')); client=s3_client(config)
    items=[]
    try:
        try: versioning=client.get_bucket_versioning(Bucket=config['bucket']).get('Status','Disabled')
        except Exception: versioning='Unknown'
        if versioning!='Disabled':
            for page in client.get_paginator('list_object_versions').paginate(Bucket=config['bucket'],Prefix='backups/'):
                for entry in page.get('Versions',[]):
                    if re.fullmatch(r'backups/[^/]+\.zip',entry['Key']):
                        items.append({'key':entry['Key'],'version_id':entry['VersionId'],'bytes':entry['Size'],'etag':entry.get('ETag')})
        else:
            for page in client.get_paginator('list_objects_v2').paginate(Bucket=config['bucket'],Prefix='backups/'):
                for entry in page.get('Contents',[]):
                    if re.fullmatch(r'backups/[^/]+\.zip',entry['Key']):
                        items.append({'key':entry['Key'],'bytes':entry['Size'],'etag':entry.get('ETag')})
    except Exception: raise ValueError('Cannot inventory S3 backups; provider diagnostics withheld') from None
    keep=sorted({x['key'] for x in items},reverse=True)[:2]
    selected=sorted((x for x in items if x['key'] not in keep),key=canonical)
    plan={'bucket':config['bucket'],'keep':keep,'items':selected,'bytes':sum(x['bytes'] for x in selected)}
    return {**plan,'confirmation':digest(canonical(plan)),
        'effect':'Permanently remove listed legacy ZIP versions only; keep latest two archive names. Attachments/restic and lifecycle untouched.'}


def s3_cleanup(root,confirmation):
    root=Path(root).resolve()
    with file_lock(root/'install.lock'):
        manager=configured(root/'memory')
        if not manager: raise ValueError('Restic must be configured')
        with memory_lock(manager),file_lock(manager.state/'operation.lock'):
            plan=s3_cleanup_preview(root)
            if confirmation!=plan['confirmation']: raise ValueError('S3 cleanup preview changed; confirm the exact current plan')
            from .backup import s3_client
            client=s3_client(json.loads((root/'backup-config.json').read_text(encoding='utf-8')))
            removed=[]
            try:
                for item in plan['items']:
                    request={'Bucket':plan['bucket'],'Key':item['key']}
                    if item.get('version_id'): request['VersionId']=item['version_id']
                    else:
                        head=client.head_object(**request)
                        if head.get('ETag')!=item['etag'] or head['ContentLength']!=item['bytes']:
                            raise ValueError('Object changed')
                    client.delete_object(**request); removed.append(item)
            except Exception:
                manager.event('s3_cleanup_failed',{'removed':removed})
                raise ValueError('S3 cleanup incomplete; re-preview before retrying. Provider diagnostics withheld') from None
            manager.event('s3_cleanup_applied',{'removed':removed,'bytes':plan['bytes']})
            return {'removed':removed,'bytes':plan['bytes']}

def activation_preview(root,target):
    root=Path(root).resolve(); target=Path(target).resolve()
    if target==root/'memory' or not target.is_relative_to(root) or target.parent!=root or target.is_symlink():
        raise ValueError('Restored target must be a direct child of the server root, separate from memory')
    receipt=json.loads((target/'restore-receipt.json').read_text(encoding='utf-8'))
    if not receipt.get('verified') or Path(receipt['target']).resolve()!=target: raise ValueError('Verified restore receipt required')
    from .backup import sha256
    checksum=sha256(target/'context.sqlite3')
    if sha256(target/'manifest.json')!=receipt.get('manifest_sha256'): raise ValueError('Restore manifest changed after verification')
    manifest=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    if checksum!=manifest.get('database_sha256'): raise ValueError('Restored database changed after verification')
    for rel,expected in manifest['files'].items():
        path=target/rel
        if path.is_symlink() or not path.resolve().is_relative_to(target) or not path.is_file() or sha256(path)!=expected:
            raise ValueError('Restored files changed after verification')
        if rel.startswith('recovered-vault/'):
            projected=target/'vault'/rel.removeprefix('recovered-vault/')
            if projected.is_symlink() or not projected.is_file() or sha256(projected)!=expected:
                raise ValueError('Restored vault changed after verification')
    with closing(sqlite3.connect((root/'memory/context.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
        workspace=con.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    if receipt['workspace_id']!=workspace: raise ValueError('Restored memory belongs to a different workspace')
    plan={'root':str(root),'target':str(target),'database_sha256':checksum,'snapshot_id':receipt['snapshot_id'],
          'effect':'Replace the active state with this snapshot; later imports must be replayed. Previous directory is retained.'}
    return {**plan,'confirmation':digest(canonical(plan))}

def activate(root,target,confirmation):
    root=Path(root).resolve(); target=Path(target).resolve()
    with file_lock(root/'install.lock'),file_lock(root/'service.lock'):
        plan=activation_preview(root,target)
        if plan['confirmation']!=confirmation: raise ValueError('Explicit approval of the current restore activation plan required')
        # Never switch beneath an old writer. The owner can reconnect clients after stopping them.
        if active_mcp(root): raise ValueError('Stop active MCP connections before activating restored memory')
        old=root/('memory-before-restore-'+uuid.uuid4().hex)
        live=root/'memory'
        manager=configured(live)
        from contextlib import nullcontext
        with memory_lock(manager) if manager else nullcontext():
            if manager: manager.checkpoint('before_restore','restore:'+uuid.uuid4().hex)
            live.rename(old)
            try: target.rename(live)
            except Exception:
                old.rename(live); raise
            if manager: manager.event('restore_activated',{'snapshot_id':plan['snapshot_id'],'previous_directory':str(old)})
        return {'activated':True,'previous_directory':str(old),'reconnect_mcp':True}


def restore_zip(root,archive,target):
    from .backup import restore,sha256
    root=Path(root).resolve(); target=Path(target).resolve()
    if target.parent!=root or target==root/'memory': raise ValueError('Use a new directory directly under server root')
    result=restore(archive,target)
    # Preserve all manual Markdown, then seal the migrated recovery image for activation preview.
    shutil.copytree(target/'recovered-vault',target/'vault',dirs_exist_ok=True)
    files={p.relative_to(target).as_posix():sha256(p) for p in target.rglob('*') if p.is_file() and p.name!='manifest.json'}
    manifest={'format':'portable-context-legacy-restored-1','database_sha256':sha256(target/'context.sqlite3'),'files':files}
    atomic_write(target/'manifest.json',canonical(manifest))
    receipt={'verified':True,'target':str(target),'snapshot_id':'zip:'+sha256(archive),
        'workspace_id':result['workspace_id'],'manifest_sha256':sha256(target/'manifest.json')}
    atomic_write(target/'restore-receipt.json',canonical(receipt))
    manager=configured(root/'memory')
    if manager: manager.event('restore_verified',receipt)
    return receipt

def active_mcp(root):
    root=Path(root).resolve(); result=[]
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            if entry.stat().st_uid!=os.getuid(): continue
            args=(entry/'cmdline').read_bytes().decode().split('\0')
            if '-m' in args and args[args.index('-m')+1]=='portable_context' and 'mcp' in args and '--home' in args:
                if Path(args[args.index('--home')+1]).resolve()==root/'memory': result.append(int(entry.name))
        except (OSError,IndexError,UnicodeError): pass
    return result

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('cleanup'); c.add_argument('--approve-plan')
    c=sub.add_parser('cleanup-s3'); c.add_argument('--approve-plan')
    c=sub.add_parser('activate'); c.add_argument('--target',required=True); c.add_argument('--approve-plan')
    c=sub.add_parser('restore-zip'); c.add_argument('--archive',required=True); c.add_argument('--target',required=True)
    args=p.parse_args(argv)
    if args.command=='cleanup': result=cleanup(args.root,args.approve_plan) if args.approve_plan else cleanup_preview(args.root)
    elif args.command=='cleanup-s3': result=s3_cleanup(args.root,args.approve_plan) if args.approve_plan else s3_cleanup_preview(args.root)
    elif args.command=='activate': result=activate(args.root,args.target,args.approve_plan) if args.approve_plan else activation_preview(args.root,args.target)
    else: result=restore_zip(args.root,args.archive,args.target)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
