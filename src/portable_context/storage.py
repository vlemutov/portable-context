"""Explicit storage accounting. Provider inventory is refreshed separately, never on every MCP read."""
import json,os,shutil,sqlite3
from pathlib import Path
from .core import canonical,atomic_write,now

def tree_bytes(root):
    root=Path(root)
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink()) if root.exists() else 0


def allocated_bytes(root):
    """Filesystem allocation where st_blocks exists; never silently equate logical and physical size."""
    root=Path(root); total=0; seen=set()
    for p in root.rglob('*') if root.exists() else []:
        if p.is_symlink() or not p.is_file(): continue
        st=p.stat()
        if not hasattr(st,'st_blocks'): return None
        identity=(st.st_dev,st.st_ino)
        if identity not in seen: total+=st.st_blocks*512;seen.add(identity)
    return total

def report(store):
    with store.connect() as con:
        try:
            tables=[dict(r) for r in con.execute('SELECT name,sum(pgsize) AS bytes FROM dbstat GROUP BY name ORDER BY bytes DESC')]
        except sqlite3.OperationalError: tables=[]
        free=con.execute('PRAGMA freelist_count').fetchone()[0]*con.execute('PRAGMA page_size').fetchone()[0]
    root=store.root.parent
    provider=root/'storage-inventory.json'
    from .restic_backup import configured
    manager=configured(store.root)
    state=manager.state if manager else root/'backup-state'
    restic={'cache_bytes':tree_bytes(state/'cache'),'staging_database_bytes':
        (state/'stage/context.sqlite3').stat().st_size if (state/'stage/context.sqlite3').exists() else 0,
        'staging_pages_bytes':tree_bytes(state/'stage/database-pages'),'staging_originals_bytes':tree_bytes(state/'stage/originals'),
        'staging_vault_bytes':tree_bytes(state/'stage/recovered-vault'),
        'journal_bytes':sum(p.stat().st_size for p in state.glob('journal.sqlite3*') if p.is_file())}
    return {'sqlite_bytes':store.db.stat().st_size,'sqlite_wal_bytes':
            (Path(str(store.db)+'-wal').stat().st_size if Path(str(store.db)+'-wal').exists() else 0),
        'sqlite_free_pages_bytes':free,'tables':tables,'originals_bytes':tree_bytes(store.root/'originals'),
        'vault_bytes':tree_bytes(store.root/'vault'),'local_backups_bytes':tree_bytes(root/'backups'),
        'migration_backups_bytes':tree_bytes(root/(store.root.name+'-migration-backups')),
        'imports_bytes':tree_bytes(root/'imports'),'releases_bytes':tree_bytes(root/'releases'),
        'restic_local_state_bytes':tree_bytes(state),'restic_breakdown':restic,
        'restic_allocated_bytes':allocated_bytes(state),
        'staging_policy':'Regenerable staging is removed after successful checkpoint; failed staging is kept until retry. Temporary backup/restore peaks still require free space.',
        'size_basis':'Logical file lengths; shared hard links can be counted in multiple directories, not allocated disk usage.',
        'restic_local_repository_bytes':tree_bytes(root/'restic-repository'),
        'disk_free_bytes':shutil.disk_usage(store.root).free,
        's3':json.loads(provider.read_text(encoding='utf-8')) if provider.exists() else {'status':'not_measured'},
        'note':'Freed SQLite pages may be reused without shrinking the file. No automatic VACUUM.'}

def forecast(store,original_bytes,source_bytes=None):
    if source_bytes is None:
        if type(original_bytes) is not int or not 0<=original_bytes<=10**15: raise ValueError('Invalid original bytes')
        return {'status':'incomplete_inputs','missing':['source_bytes'],'fits_with_20_percent_reserve':None,
                'note':'Supply record-part and manifest bytes from prepare; omitted metadata size is not zero.'}
    if any(type(v) is not int or not 0<=v<=10**15 for v in (original_bytes,source_bytes)):
        raise ValueError('Byte counts must be nonnegative integers')
    # Conservative initial range; observed ratios are not a universal expansion constant.
    low=original_bytes+2*source_bytes; high=original_bytes+6*source_bytes
    peak=3*original_bytes+2*6*source_bytes+store.db.stat().st_size
    free=shutil.disk_usage(store.root).free
    return {'status':'estimated','input_unique_blob_bytes':original_bytes,'input_source_bytes':source_bytes,
        'memory_growth_range_bytes':[low,high],'additional_peak_bytes':peak,'disk_free_bytes':free,
        'fits_with_20_percent_reserve':peak<=free*0.8,
        's3_new_original_copies_up_to_bytes':2*original_bytes,
        'assumptions':'Includes upload staging, local originals and restic staging. S3 may hold operational and backup originals. '
                      'Source metadata expansion estimate 2..6x; shared hashes can reduce storage. Old versions and retention add overhead.'}

def refresh_s3(store,config_path):
    from .backup import s3_client
    config=json.loads(Path(config_path).read_text(encoding='utf-8')); client=s3_client(config)
    groups={name:{'current_bytes':0,'noncurrent_bytes':0,'objects':0,'delete_markers':0} for name in ('attachments','backups','restic','other')}
    try:
        try: versioning=client.get_bucket_versioning(Bucket=config['bucket']).get('Status','Disabled')
        except Exception: versioning='Unknown'
        # Object-reader credentials can list versions without reading administrative bucket settings.
        if versioning!='Disabled':
            pages=client.get_paginator('list_object_versions').paginate(Bucket=config['bucket'])
            for page in pages:
                for item in page.get('Versions',[]):
                    group=groups.get(item['Key'].split('/')[0],groups['other'])
                    group['current_bytes' if item['IsLatest'] else 'noncurrent_bytes']+=item['Size']; group['objects']+=1
                for item in page.get('DeleteMarkers',[]): groups.get(item['Key'].split('/')[0],groups['other'])['delete_markers']+=1
        else:
            for page in client.get_paginator('list_objects_v2').paginate(Bucket=config['bucket']):
                for item in page.get('Contents',[]):
                    group=groups.get(item['Key'].split('/')[0],groups['other']); group['current_bytes']+=item['Size']; group['objects']+=1
        result={'status':'measured','at':now(),'versioning':versioning,'version_inventory':versioning!='Disabled','prefixes':groups}
    except Exception:
        result={'status':'unavailable','at':now(),'reason':'Check provider permissions/connectivity; credentials and diagnostics withheld'}
    atomic_write(store.root.parent/'storage-inventory.json',canonical(result))
    return result
