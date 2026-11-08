"""Exact recovery inventories. Time alone is never evidence that a package is recoverable."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .core import canonical,digest,key


def inventory(con):
    tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'revisions':[list(r) for r in con.execute('SELECT id,hash FROM revisions ORDER BY id,hash')],
            'originals':[r[0] for r in con.execute('SELECT hash FROM imports ORDER BY hash')],
            'project_occurrences':[r[0] for r in con.execute('SELECT occurrence_id FROM project_file_versions ORDER BY occurrence_id')] if 'project_file_versions' in tables else []}


def verified(manager):
    if not manager: return []
    existing={s['id'] for s in manager.snapshots()}
    results=[]
    with manager.connect() as con:
        seals={json.loads(r[0]).get('snapshot_id'):json.loads(r[0]).get('coverage_sha256') for r in con.execute(
            "SELECT details FROM events WHERE kind='coverage_verified' ORDER BY seq")}
    for snapshot,seal in seals.items():
        path=manager.state/'coverage'/(snapshot+'.json')
        if snapshot not in existing or not path.is_file() or path.is_symlink(): continue
        text=path.read_text(encoding='utf-8')
        if digest(text)!=seal: continue
        results.append({'snapshot_id':snapshot,**json.loads(text)})
    return results


def package_requirements(directory,con):
    from .capture import load_package
    manifest,records=load_package(directory)
    versions=[]
    for record in records:
        r=dict(record); meta=r.pop('conversation_metadata',None)
        if r['source']=='claude-code' and meta:
            row=con.execute('SELECT conversation_id FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                            (r['source'],r['instance'],meta.get('native_id',r['conversation_id']))).fetchone()
            if row: r['conversation_id']=row[0]
            mid=key(r['source'],r['instance'],r['conversation_id'],r['message_id'])
            title=con.execute('SELECT title FROM messages WHERE id=?',(mid,)).fetchone()
            r['title']=title[0] if title else r['conversation_id']
        versions.append([key(r['source'],r['instance'],r['conversation_id'],r['message_id']),digest(canonical(r))])
    originals={b['sha256'] for b in manifest.get('blobs',[])}
    for r in records:
        originals.update(a['sha256'] for a in r['attachments'] if isinstance(a,dict) and a.get('export_observation',{}).get('blob'))
    snap=manifest.get('project_snapshot'); occurrences=[]
    if snap:
        for a in snap['files']:
            fid=key('project-file',snap['project_id'],snap['binding_id'],a['relative_path'])
            occurrences.append(key('project-occurrence',fid,digest(canonical(a))))
    return {'revisions':versions,'originals':sorted(originals),'project_occurrences':occurrences}


def covers(available,required):
    return (set(map(tuple,required['revisions']))<=set(map(tuple,available['revisions']))
            and set(required['originals'])<=set(available['originals'])
            and set(required['project_occurrences'])<=set(available['project_occurrences']))
