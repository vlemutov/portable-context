"""Bounded resumable MCP uploads. Client paths never grant server filesystem access."""
import base64
import hashlib
import json
from pathlib import Path
import re

from .core import canonical, digest, atomic_write
from .capture import import_capture

MAX_CHUNK=1024*1024


def validate_manifest(manifest):
    if manifest.get('format')!='portable-context-capture-2':
        raise ValueError('Upload requires capture protocol 2; upgrade the client')
    if len(canonical(manifest).encode())>8*1024*1024:
        raise ValueError('Manifest exceeds 8 MiB')
    files={}
    for part in manifest.get('parts',[])+manifest.get('blobs',[]):
        name=part.get('path','')
        if not re.fullmatch(r'(?:records(?:-\d+)?\.json|blobs/[0-9a-f]{64})',name):
            raise ValueError('Invalid upload path')
        sha=part.get('sha256','')
        size=part.get('size')
        if not re.fullmatch('[0-9a-f]{64}',sha) or type(size) is not int or not 0<=size<=512*1024*1024:
            raise ValueError('Invalid upload file description')
        if name.startswith('blobs/') and name!='blobs/'+sha:
            raise ValueError('Blob filename/hash mismatch')
        if name in files:
            raise ValueError('Duplicate upload file')
        files[name]=part
    if not manifest.get('parts') or len(files)>100000 or sum(p['size'] for p in files.values())>20*1024**3:
        raise ValueError('Package exceeds upload limits')
    return files


class Uploads:
    def __init__(self,workspace):
        self.workspace=workspace
        if not workspace.allowed_roots:
            raise ValueError('No upload directory configured on server')
        self.root=workspace.allowed_roots[0]/'uploads'
        self.root.mkdir(parents=True,exist_ok=True)

    def folder(self,identifier):
        if not re.fullmatch('[0-9a-f]{64}',identifier):
            raise ValueError('Invalid upload ID')
        p=self.root/identifier
        if not p.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Unsafe upload directory')
        return p

    def begin(self,manifest,project_id=None,executor=None,run_id=None):
        files=validate_manifest(manifest)
        identifier=digest(canonical(manifest))
        folder=self.folder(identifier)
        from .backup import sha256
        known={}
        with self.workspace.store.connect() as con:
            from .policy import check_manifest
            check_manifest(con,manifest,project_id)
            for part in manifest.get('blobs',[]):
                old=self.workspace.store.root/'originals'/part['sha256']
                row=con.execute('SELECT size FROM imports WHERE hash=?',(part['sha256'],)).fetchone()
                if row and row[0]==part['size'] and not old.is_symlink() and old.is_file() and sha256(old)==part['sha256']:
                    known[part['path']]=old
        from .storage import forecast
        estimate=forecast(self.workspace.store,sum(p['size'] for p in manifest.get('blobs',[]) if p['path'] not in known),
            sum(p['size'] for p in manifest.get('parts',[]))+len(canonical(manifest).encode('utf-8')))
        if not estimate['fits_with_20_percent_reserve']:
            raise ValueError('Insufficient server space for this capture with reserve; inspect forecast_import and storage_report')
        with self.workspace.store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            from .policy import check_manifest
            from .import_log import start
            check_manifest(con,manifest,project_id)
            run_id=start(con,identifier,executor or manifest.get('executor'),run_id=run_id)
            folder.mkdir(exist_ok=True)
            atomic_write(folder/'manifest.json',canonical(manifest))
            target=folder/'target.json'
            selection={'project_id':project_id}
            if target.exists() and json.loads(target.read_text())!=selection:
                raise ValueError('Package already has another target project; use explicit project reassignment')
            atomic_write(target,canonical(selection))
            for name,old in known.items():
                destination=folder/name
                if destination.exists(): continue
                destination.parent.mkdir(exist_ok=True)
                # A verified immutable original can satisfy a new package without network transfer.
                # Upload retries only compare existing bytes; they never overwrite this hard link.
                import os,shutil
                try: os.link(old,destination)
                except OSError: shutil.copyfile(old,destination)
        return {'upload_id':identifier,'run_id':run_id,'files':len(files),'max_chunk_bytes':MAX_CHUNK,'reused_blobs':len(known)}

    def preflight(self,folder,con):
        from .policy import check_manifest
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        target=folder/'target.json'
        project_id=json.loads(target.read_text())['project_id'] if target.exists() else None
        check_manifest(con,manifest,project_id)
        return project_id

    def status(self,identifier,offset=0,limit=100):
        folder=self.folder(identifier)
        files=validate_manifest(json.loads((folder/'manifest.json').read_text(encoding='utf-8')))
        items=[]
        names=sorted(files)
        offset=max(0,offset)
        limit=max(1,min(limit,500))
        for name in names[offset:offset+limit]:
            p=folder/name
            if not p.resolve().is_relative_to(folder):
                raise ValueError('Unsafe upload member')
            size=p.stat().st_size if p.exists() else 0
            verified=False
            if p.exists() and size==files[name]['size']:
                from .backup import sha256
                verified=sha256(p)==files[name]['sha256']
            items.append({'path':name,'offset':size,'verified':verified,'size':files[name]['size']})
        return {'files':items,'next_offset':offset+limit if offset+limit<len(names) else None}

    def put(self,identifier,chunks):
        if not isinstance(chunks,list) or not 1<=len(chunks)<=100:
            raise ValueError('Expected 1..100 chunks')
        if sum(len(c.get('content_base64','')) for c in chunks)>MAX_CHUNK*4//3+1024:
            raise ValueError('Chunk batch exceeds 1 MiB')
        folder=self.folder(identifier)
        files=validate_manifest(json.loads((folder/'manifest.json').read_text(encoding='utf-8')))
        replies=[]
        with self.workspace.store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self.preflight(folder,con)
            for chunk in chunks:
                name=chunk['path']
                if name not in files:
                    raise ValueError('File not declared in manifest')
                p=folder/name
                if not p.resolve().is_relative_to(folder):
                    raise ValueError('Unsafe upload member')
                data=base64.b64decode(chunk['content_base64'],validate=True)
                offset=chunk['offset']
                if type(offset) is not int or offset<0 or offset+len(data)>files[name]['size']:
                    raise ValueError('Invalid upload offset or size')
                p.parent.mkdir(parents=True,exist_ok=True)
                size=p.stat().st_size if p.exists() else 0
                if offset>size:
                    raise ValueError('Missing preceding upload chunk')
                if offset<size:
                    with p.open('rb') as stream:
                        stream.seek(offset)
                        if stream.read(len(data))!=data:
                            raise ValueError('Conflicting upload retry')
                else:
                    with p.open('ab') as stream:
                        stream.write(data)
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                replies.append({'path':name,'offset':p.stat().st_size})
        return {'files':replies}

    def finish(self,identifier,project_id=None,run_id=None):
        folder=self.folder(identifier)
        with self.workspace.store.connect() as con:
            selected=self.preflight(folder,con)
            if selected!=project_id:
                raise ValueError('Target project differs from preflight; begin the upload with its target project')
        # Capture preflight validates every record and blob before modifying memory.
        try:
            result=import_capture(self.workspace.store,folder,self.workspace.allowed_roots,project_id,transfer_limit=25,run_id=run_id)
        except Exception:
            if run_id:
                from .import_log import progress
                with self.workspace.store.connect() as con:
                    progress(con,run_id,dict(inserted=0,revised=0,unchanged=0),[],error='capture_import_failed')
            raise
        return compact_receipt(result)


def compact_receipt(result):
    from collections import Counter
    return {k:v for k,v in result.items() if k!='attachments'} | {
        'stored_attachment_occurrences':len(result.get('attachments',[])),
        's3':dict(Counter(x['s3']['status'] for x in result.get('attachments',[])))}
