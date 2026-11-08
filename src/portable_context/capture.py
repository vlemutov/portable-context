"""Freeze selected normalized conversations and local attachments BEFORE source access is lost.

Cross-platform client command: python -m portable_context.capture --help.
No directory crawling, URL fetching, hidden history discovery, or automatic semantic inference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import base64
import re

from .core import Store, canonical, digest, key, now, validate
from .catalog import Catalog, project_catalog
from .privacy import screen_value, screen_bytes
from .core import guarded_import


def capture(records, destination, allowed_roots=(), memory_project=None, max_file_bytes=512 * 1024 * 1024,
            secret_policy='block', part_bytes=4 * 1024 * 1024, adapter_report=None, policy=None, include_paths=(), executor=None):
    from .policy import Policy,agent_workspace,runtime_exclusion
    from .control import category
    from . import __version__
    policy=policy or Policy.load(None)
    explicitly_included={str(Path(p).expanduser().resolve()) for p in include_paths}
    if secret_policy not in ('block', 'redact'):
        raise ValueError('secret_policy must be block or redact')
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError('Capture destination already exists; keep immutable export snapshots separate')
    roots = [Path(p).expanduser().resolve() for p in allowed_roots]
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {'stored_occurrences': 0, 'missing_occurrences': 0, 'unique_bytes': 0, 'unique_blobs': 0,
              'redacted_records': 0, 'quarantined_files': 0, 'opaque_files': 0, 'excluded_thinking_chars': 0,
              'omitted_chars': 0, 'screening': 'best_effort_local', 'secret_policy': secret_policy,
              'excluded_records':0,'excluded_occurrences':0,'exclusion_rules':{}}
    scopes={}
    def excluded(reason):
        report['excluded_occurrences']+=1
        report['exclusion_rules'][reason]=report['exclusion_rules'].get(reason,0)+1
    with tempfile.TemporaryDirectory(prefix='.capture-', dir=destination.parent) as tmp:
        folder = Path(tmp) / 'package'
        (folder / 'blobs').mkdir(parents=True)
        normalized = []
        for original in records:
            original=json.loads(canonical(original))
            if memory_project is not None:
                original['memory_project']=memory_project
            permitted,rules=policy.select(original)
            if not permitted['conversation']:
                report['excluded_records']+=1
                for rule in rules or ['default']:
                    report['exclusion_rules'][rule]=report['exclusion_rules'].get(rule,0)+1
                continue
            context={'cwd':(original.get('raw') or {}).get('cwd'),
                     'project':(original.get('memory_project') or {}).get('id')}
            original['capture_context']=context
            if not permitted['tool_payloads']:
                original['raw']={}
                if original.get('role')=='tool':
                    original['text']=''
            for payload in original.get('payloads',[]):
                if not permitted['document_bytes' if payload.get('kind')=='image' else 'tool_payloads']:
                    payload['value']=None
            r, findings = screen_value(json.loads(canonical(original)), secret_policy=='redact')
            if findings and secret_policy=='block':
                raise ValueError('Secret screening blocked record; categories: '+','.join(sorted(findings)))
            if findings:
                report['redacted_records'] += 1
                r.setdefault('completeness',{})['redacted'] = True
            r = validate(r)
            # Preserve large fields in the object store, without repeating them inside every record part.
            for field in ('raw','text'):
                if permitted['tool_payloads'] and len(canonical(r[field]).encode('utf-8'))>32768:
                    r.setdefault('payloads',[]).append({'block_index':field,'kind':'full_'+field,'value':r[field]})
                    r[field]=({'archived_payload':'payload:'+r['message_id']+':'+field} if field=='raw' else r[field][:2000]+'\n[Full text archived as payload:'+r['message_id']+':text]')
                    r.setdefault('completeness',{})[field+'_archived']=True
            if memory_project is not None:
                r['memory_project'] = memory_project
            r = validate(r)
            report['excluded_thinking_chars'] += r.get('completeness',{}).get('excluded_thinking_chars',0)
            report['omitted_chars'] += (r.get('raw') or {}).get('omitted_chars',0)
            attachments = []
            payload_ids=set()
            for i,payload in enumerate(r.pop('payloads', [])):
                if not isinstance(payload.get('kind'),str) or not payload['kind'].strip():
                    raise ValueError('New source payloads require an explicit kind; use unknown for unsupported source shapes')
                payload_id='payload:'+r['message_id']+':'+str(payload.get('block_index',i))
                if payload_id in payload_ids:
                    raise ValueError('Duplicate payload identity within one message')
                payload_ids.add(payload_id)
                kind=payload.get('kind','payload')
                payload_category='document' if kind=='image' else 'source_payload'
                if not permitted['document_bytes' if payload_category=='document' else 'tool_payloads']:
                    attachments.append({'file_id':payload_id,'name':kind+'.json','kind':kind,
                        'category':payload_category,'availability':'excluded',
                        'export_observation':{'reason':'capture_policy','method':'embedded_source_payload'}})
                    excluded('capture_policy')
                    continue
                value=payload['value']
                data=canonical(value).encode('utf-8')
                name=payload.get('kind','payload')+'.json'
                if isinstance(value,dict) and value.get('type')=='image' and value.get('source',{}).get('type')=='base64':
                    data=base64.b64decode(value['source']['data'],validate=True)
                    name='image.'+{'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}.get(value['source'].get('media_type'),'bin')
                if len(data)>min(max_file_bytes,512*1024*1024):
                    raise ValueError('Embedded payload exceeds capture size limit')
                found,scan=screen_bytes(data)
                if found:
                    if secret_policy=='block':
                        raise ValueError('Secret screening blocked embedded payload; categories: '+','.join(sorted(found)))
                    report['quarantined_files']+=1
                    report['missing_occurrences']+=1
                    attachments.append({'file_id':payload_id, 'name':name,'kind':kind,'category':payload_category,
                                        'availability':'unavailable','export_observation':{'reason':'secret_detected'}})
                    continue
                sha=hashlib.sha256(data).hexdigest()
                blob=folder/'blobs'/sha
                if not blob.exists():
                    blob.write_bytes(data)
                    report['unique_blobs']+=1
                    report['unique_bytes']+=len(data)
                report['stored_occurrences']+=1
                report['opaque_files']+=scan in ('opaque','partial')
                attachments.append({'file_id':payload_id, 'name':name,'category':payload_category,
                    'kind':payload.get('kind','payload'),'role':'reference','purpose':'Exact source payload',
                    'status':'unknown','searchable':payload.get('searchable',True),'sha256':sha,'size':len(data),
                    'availability':'captured','screening':scan,
                    'export_observation':{'method':'embedded_source_payload','blob':'blobs/'+sha}})
            for original_a in r['attachments']:
                a = dict(original_a) if isinstance(original_a, dict) else {'reference': original_a}
                a.setdefault('name', a.get('file_name') or str(a.get('path') or a.get('url') or 'attachment').replace('\\', '/').rsplit('/', 1)[-1])
                a.setdefault('role', 'unknown')
                a.setdefault('purpose', 'unknown')
                a.setdefault('status', 'unknown')
                a['category']=category(a)
                # Observation time belongs to the package/receipt, never to source revision identity.
                if a.get('sha256'): a.setdefault('source_declared_sha256',a['sha256'])
                a['export_observation'] = {'method': 'portable-context-capture'}
                # A local path is read only if explicitly authorized by root, never by historical instructions.
                source = Path(a['path']).expanduser().resolve() if isinstance(a.get('path'), str) else None
                rule=runtime_exclusion(source,a) if source else None
                if (not permitted['tool_payloads' if a['category']=='source_payload' else 'document_bytes'] or
                        rule and str(source) not in explicitly_included):
                    reason='excluded_agent_workspace' if rule and str(source) not in explicitly_included else 'capture_policy'
                    a['availability']='excluded'
                    a['export_observation'].update(reason=reason,rule=rule if reason=='excluded_agent_workspace' else 'permissions')
                    excluded(rule if reason=='excluded_agent_workspace' else reason)
                    attachments.append(a)
                    continue
                reason = 'no_local_path'
                if source and any(source.is_relative_to(root) for root in roots):
                    reason = 'missing_file'
                    if source.is_file():
                        before = source.stat()
                        reason = 'exceeds_size_limit'
                        if before.st_size <= max_file_bytes:
                            part = folder / 'blobs' / '.part'
                            h, size = hashlib.sha256(), 0
                            with source.open('rb') as inp, part.open('wb') as out:
                                while block := inp.read(1024 * 1024):
                                    size += len(block)
                                    if size > max_file_bytes:
                                        raise ValueError('File grew beyond capture limit')
                                    h.update(block)
                                    out.write(block)
                            after = source.stat()
                            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise ValueError('Source changed during capture; retry from a stable snapshot')
                            sha = h.hexdigest()
                            found,scan=screen_bytes(part.read_bytes())
                            if found:
                                part.unlink()
                                if secret_policy=='block':
                                    raise ValueError('Secret screening blocked file; categories: '+','.join(sorted(found)))
                                a.update(availability='unavailable')
                                a['export_observation']['reason']='secret_detected'
                                report['quarantined_files']+=1
                                report['missing_occurrences']+=1
                                attachments.append(a)
                                continue
                            a['screening']=scan
                            report['opaque_files']+=scan in ('opaque','partial')
                            if a.get('sha256') and a['sha256'] != sha:
                                raise ValueError('Source bytes differ from declared attachment checksum')
                            blob = folder / 'blobs' / sha
                            if blob.exists():
                                part.unlink()
                            else:
                                part.replace(blob)
                                report['unique_blobs'] += 1
                                report['unique_bytes'] += size
                            a.update(sha256=sha, size=size, availability='captured')
                            a['export_observation'].update(blob='blobs/' + sha)
                            report['stored_occurrences'] += 1
                            if agent_workspace(source) and not rule:
                                report['included_runtime_files']=report.get('included_runtime_files',0)+1
                            attachments.append(a)
                            continue
                elif source:
                    reason = 'outside_allowed_roots'
                a['availability'] = 'unavailable'
                a['export_observation']['reason'] = reason
                report['missing_occurrences'] += 1
                attachments.append(a)
            r['attachments'] = attachments
            scope={k:r[k] for k in ('source','instance','conversation_id')}
            for field in ('project','memory_project'):
                if field in r:
                    scope[field]=r[field]
            sid=canonical(scope)
            if sid not in scopes:
                scopes[sid]={**scope,'permissions':dict(permitted)}
            else:
                for axis in permitted:
                    scopes[sid]['permissions'][axis] |= permitted[axis]
            normalized.append(r)
        # Passports and entry pages exist inside the capture itself, before remote import.
        draft = Store(folder / 'catalog')
        draft.ingest(normalized, 'client-capture')
        parts=[]
        batch=[]
        size=2
        def emit():
            data=canonical(batch).encode('utf-8')
            name='records.json' if not parts else 'records-'+str(len(parts))+'.json'
            (folder/name).write_bytes(data)
            parts.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'messages':len(batch)})
        for r in normalized:
            n=len(canonical(r).encode('utf-8'))+1
            if n>32*1024*1024:
                raise ValueError('Single record exceeds 32 MiB; externalize large content first')
            if batch and size+n>part_bytes:
                emit()
                batch=[]
                size=2
            batch.append(r)
            size+=n
        if batch or not parts:
            emit()
        blobs=[{'path':'blobs/'+p.name,'sha256':p.name,'size':p.stat().st_size} for p in sorted((folder/'blobs').iterdir())]
        category_hashes={'document':{},'source_payload':{}}
        for r in normalized:
            for a in r['attachments']:
                if a.get('sha256') and a.get('size') is not None:
                    category_hashes[category(a)][a['sha256']]=a['size']
        report.update(record_parts_bytes=sum(p['size'] for p in parts),
                      unique_blob_bytes_by_category={k:sum(v.values()) for k,v in category_hashes.items()},
                      package_payload_bytes=sum(p['size'] for p in parts+blobs))
        manifest = {'format': 'portable-context-capture-2', 'created_at': now(),
                    'parts':parts,'blobs':blobs,
                    'sources':[{'source':s,'instance':i} for s,i in sorted({(r['source'],r['instance']) for r in normalized})],
                    'messages': len(normalized), **report, 'capture_scopes':list(scopes.values()),
                    'policy_fingerprint':policy.fingerprint,
                    'executor':executor or {'client':'portable-context','version':__version__,'agent':None},
                    'adapter_report':adapter_report,
                    'note': 'These are currently accessible file bytes; original message-time bytes cannot be inferred from a mutable path.'}
        (folder / 'manifest.json').write_text(canonical(manifest), encoding='utf-8')
        # The draft catalog contains reference passports; remote transfer binds captured hashes.
        folder.rename(destination)
    return {'directory': str(destination), **manifest}


@guarded_import
def import_capture(store, directory, allowed_roots, project_id=None, transfer_limit=None, run_id=None):
    from time import monotonic
    clock=monotonic()
    directory = Path(directory).expanduser().resolve()
    roots = [Path(p).expanduser().resolve() for p in allowed_roots]
    if not any(directory.is_relative_to(root) for root in roots):
        raise ValueError('Capture must be under an allowed server source root')
    from .workspace import Workspace
    workspace=Workspace(store,allowed_roots,reconcile=False)
    manifest_path=directory/'manifest.json'
    if not manifest_path.resolve().is_relative_to(directory) or manifest_path.stat().st_size>8*1024*1024:
        raise ValueError('Unsafe or oversized manifest')
    package_id=digest(canonical(json.loads(manifest_path.read_text(encoding='utf-8'))))
    with store.connect() as con:
        old=con.execute('SELECT report FROM capture_receipts WHERE id=?',(package_id,)).fetchone()
        cached=con.execute('SELECT * FROM capture_run_state WHERE run_id=?',(run_id,)).fetchone() if run_id else None
    previous=json.loads(old[0]) if old else None
    # Full preflight on first import. On resume, each pending transfer verifies its own SHA256;
    # re-hashing already committed originals on every 25-file step is quadratic work.
    if cached:
        if cached['package_id']!=package_id: raise ValueError('Import run belongs to another package')
        if project_id and cached['project_id']!=project_id: raise ValueError('Import run project changed')
        project_id=cached['project_id']
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        state=json.loads(cached['state']); records=state['policy_records']
        from .backup import sha256
        parts=manifest.get('parts',[{'path':'records.json','sha256':manifest.get('records_sha256')}])
        for part in parts:
            path=directory/part['path']
            if not path.resolve().is_relative_to(directory) or sha256(path)!=part['sha256']:
                raise ValueError('Capture record checksum mismatch on resume')
    else:
        manifest,records=load_package(directory,verify_blobs=previous is None)
    selections={r['memory_project']['id'] for r in records if r.get('memory_project',{}).get('existing') is True}
    if selections:
        if len(selections)!=1 or project_id and project_id not in selections:
            raise ValueError('Capture contains conflicting existing-project selections')
        project_id=next(iter(selections))
    if manifest.get('project_snapshot'):
        if project_id and project_id!=manifest['project_snapshot']['project_id']:
            raise ValueError('Project snapshot target differs from selected project')
        from .project_files import import_snapshot
        return import_snapshot(store,directory,manifest,run_id,transfer_limit)
    if project_id:
        Catalog(store).project(project_id)
    from .policy import check_record,check_manifest
    from .control import record_session
    from .import_log import start,progress,completed_batch,metrics,totals as run_totals
    session_metadata=[r.get('conversation_metadata') for r in records]
    import uuid
    run_id=run_id or str(uuid.uuid4())
    from .restic_backup import protection
    checkpoint_started=monotonic()
    protection(store.root,run_id)
    checkpoint_elapsed=monotonic()-checkpoint_started
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        check_manifest(con,manifest,project_id)
        for r in records:
            check_record(con,r,project_id)
        if not cached: records=[record_session(con,r) for r in records]
        run_id=start(con,package_id,manifest.get('executor'),run_id)
        metrics(con,run_id,'checkpoint',checkpoint_elapsed)
        metrics(con,run_id,'validate',monotonic()-clock-checkpoint_elapsed)
    # A bundle may reference ONLY its own hashed blobs, never arbitrary source paths.
    transfers = []
    for r in ([] if cached else records):
        for pos, a in enumerate(r['attachments']):
            if not isinstance(a, dict) or not a.get('export_observation', {}).get('blob'):
                continue
            sha = a.get('sha256', '')
            import re
            if not re.fullmatch('[0-9a-f]{64}', sha) or a['export_observation']['blob'] != 'blobs/' + sha:
                raise ValueError('Invalid capture blob reference')
            path = (directory / 'blobs' / sha).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise ValueError('Capture blob missing or outside package')
            mid = key(r['source'], r['instance'], r['conversation_id'], r['message_id'])
            transfers.append((key('occurrence', mid, digest(canonical(r)), pos), path, sha))
    if cached:
        transfers=[(oid,directory/'blobs'/sha,sha) for oid,sha in state['transfers']]
    totals = {'inserted': 0, 'revised': 0, 'unchanged': 0}
    clock=monotonic()
    for start in range(0, 0 if cached else len(records), 100):
        with store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            batch_key=digest(canonical(records[start:start+100]))
            if completed_batch(con,run_id,batch_key):
                continue
            if project_id:
                for r in records[start:start+100]:
                    con.execute("INSERT INTO catalog_assignments VALUES (?,?,'agent_assignment') ON CONFLICT(conversation) DO UPDATE SET "
                                "project_id=excluded.project_id,basis=excluded.basis WHERE catalog_assignments.project_id!=excluded.project_id",
                                (key(r['source'],r['instance'],r['conversation_id']),project_id))
            batch=[dict(r,conversation_metadata=meta) if meta else r
                   for r,meta in zip(records[start:start+100],session_metadata[start:start+100])]
            result = store._ingest(con, batch, 'capture:' + package_id,blob_root=directory/'blobs')
            progress(con,run_id,result,records[start:start+100],accumulate=True,
                     batch_key=batch_key)
        for k in totals:
            totals[k] += result[k]
    with store.connect() as con:
        metrics(con,run_id,'ingest',monotonic()-clock,records_ingested=sum(totals.values()))
        cumulative=run_totals(con,run_id)['messages']
        if not cached:
            policy_records={}
            for r in records:
                summary={k:r[k] for k in ('source','instance','conversation_id','project','memory_project','role') if k in r}
                summary.update(raw={'present':True} if r.get('raw') else {},text='present' if r.get('text') else '')
                from .control import category
                byte_categories={category(a) for a in r['attachments'] if isinstance(a,dict) and (
                    a.get('content_base64') or a.get('export_observation',{}).get('blob') or
                    a.get('sha256') and a.get('availability') not in ('excluded','unavailable','reference_only'))}
                summary['attachments']=[{'category':c,'sha256':'present','availability':'captured'} for c in sorted(byte_categories)]
                policy_records[canonical(summary)]=summary
            state={'policy_records':list(policy_records.values()),'transfers':[(oid,sha) for oid,_,sha in transfers]}
            con.execute('INSERT OR REPLACE INTO capture_run_state VALUES (?,?,?,?)',(run_id,package_id,project_id,canonical(state)))
    catalog = Catalog(store, [directory])
    # A previous run's receipt is not a current proof for this S3 target. New runs
    # traverse sync_blob (cheap while its scoped proof is valid); only resume the
    # same run without repeating transfers already committed by that run.
    receipts={r['occurrence_id']:r for r in previous.get('attachments',[])} if previous and previous.get('run_id')==run_id else {}
    pending=[t for t in transfers if t[0] not in receipts or receipts[t[0]]['s3']['status']=='failed']
    from concurrent.futures import ThreadPoolExecutor
    grouped={}
    for oid,path,sha in pending: grouped.setdefault(sha,[]).append((oid,path))
    selected=list(grouped.items())[:transfer_limit] if transfer_limit else list(grouped.items())
    clock=monotonic()
    def transfer(item):
        sha,occurrences=item
        return catalog.store_shared(occurrences,sha)
    worker_timings={}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for batch_receipts in executor.map(transfer,selected):
            for receipt in batch_receipts: receipts[receipt['occurrence_id']]=receipt
            if batch_receipts:
                for phase,elapsed in batch_receipts[0].get('timings_seconds',{}).items():
                    worker_timings[phase]=worker_timings.get(phase,0)+elapsed
    complete=len(receipts)==len(transfers) and all(r['s3']['status']!='failed' for r in receipts.values())
    with store.connect() as con:
        for phase,elapsed in worker_timings.items(): metrics(con,run_id,'worker_'+phase,elapsed)
        metrics(con,run_id,'transfer',monotonic()-clock,unique_blob_attempts=len(selected),
                attachment_attempts=sum(len(items) for _,items in selected))
    clock=monotonic()
    if complete: workspace.reconcile_sources()
    if project_id and (not previous or previous.get('project_id')!=project_id):
        p=catalog.project(project_id)
        conversations=sorted({key(r['source'],r['instance'],r['conversation_id']) for r in records})
        for start in range(0,len(conversations),500):
            catalog.update_project(project_id,p['title'],p['summary'],conversations[start:start+500],p['version'],'capture-import')
            p=catalog.project(project_id)
    if complete:
        project_catalog(store)
    result = {'messages': cumulative, 'pass_messages':totals, 'initial_messages':cumulative,
            'attachments': list(receipts.values()), 'missing_at_capture': manifest.get('missing_occurrences'),
            'complete':complete,'remaining_transfers':sum(t[0] not in receipts or receipts[t[0]]['s3']['status']=='failed' for t in transfers),'project_id':project_id,
            'status':'complete' if complete else 'in_progress',
            'package_id':package_id,'run_id':run_id, 'captured_at':manifest.get('created_at'),
            'completeness':{k:manifest.get(k,0) for k in ('missing_occurrences','omitted_chars','excluded_thinking_chars','redacted_records','quarantined_files','opaque_files','excluded_records','excluded_occurrences','exclusion_rules')},
            'note': 'Safe to resume the same immutable package; inspect each S3 status separately.'}
    with store.connect() as con:
        metrics(con,run_id,'projection' if complete else 'awaiting_transfer',monotonic()-clock)
        details=run_totals(con,run_id)
        result.update(timings_seconds=details.get('timings_seconds'),work=details.get('work'),phase='complete' if complete else 'transfer')
        progress(con,run_id,totals,records,complete=complete,receipt=result)
        con.execute('INSERT OR REPLACE INTO capture_receipts VALUES (?,?,?)',(result['package_id'],now(),canonical(result)))
        if complete and manifest.get('repaired_from'):
            old=con.execute('SELECT report FROM capture_receipts WHERE id=?',(manifest['repaired_from'],)).fetchone()
            if old:
                superseded=json.loads(old[0])
                superseded.update(status='superseded',superseded_by=package_id)
                con.execute('UPDATE capture_receipts SET report=? WHERE id=?',(canonical(superseded),manifest['repaired_from']))
    return result


def load_package(directory,verify_blobs=True):
    directory=Path(directory).resolve()
    path=directory/'manifest.json'
    if not path.resolve().is_relative_to(directory) or path.stat().st_size>8*1024*1024:
        raise ValueError('Unsafe or oversized manifest')
    m=json.loads(path.read_text(encoding='utf-8'))
    if m.get('format')=='portable-context-capture-1':
        parts=[{'path':'records.json','sha256':m['records_sha256']}]
    elif m.get('format')=='portable-context-capture-2':
        parts=m['parts']
    else:
        raise ValueError('Unsupported capture format')
    records=[]
    for part in parts:
        if not re.fullmatch(r'records(?:-\d+)?\.json',part['path']):
            raise ValueError('Invalid record part path')
        path=directory/part['path']
        if not path.resolve().is_relative_to(directory) or path.stat().st_size>32*1024*1024:
            raise ValueError('Unsafe or oversized record part')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=part['sha256']:
            raise ValueError('Capture record checksum mismatch')
        records.extend(validate(r) for r in json.loads(data))
    if len(records)!=m['messages']:
        raise ValueError('Capture record count mismatch')
    # Preflight ALL bytes before any message is committed.
    checked=set()
    for r in records:
        for a in r['attachments']:
            if isinstance(a,dict) and a.get('export_observation',{}).get('blob'):
                sha=a.get('sha256','')
                if not re.fullmatch('[0-9a-f]{64}',sha) or a['export_observation']['blob']!='blobs/'+sha:
                    raise ValueError('Invalid blob reference')
                p=directory/'blobs'/sha
                if not p.resolve().is_relative_to(directory) or not p.is_file() or p.stat().st_size>512*1024*1024:
                    raise ValueError('Unsafe or missing blob')
                from .backup import sha256
                if verify_blobs and sha not in checked and sha256(p)!=sha:
                    raise ValueError('Capture blob checksum mismatch')
                checked.add(sha)
    return m,records


def upgrade_capture(directory,destination):
    """Repair 0.5.0 generated payload IDs from an immutable snapshot, preserving captured bytes."""
    import shutil
    directory=Path(directory).resolve()
    destination=Path(destination).resolve()
    if destination.exists():
        raise ValueError('Upgrade requires a new destination; original capture is retained')
    manifest,records=load_package(directory)
    if manifest.get('format')!='portable-context-capture-2':
        raise ValueError('This repair targets capture-v2 packages from 0.5.0')
    changed=0
    for r in records:
        for a in r['attachments']:
            if not isinstance(a,dict) or a.get('export_observation',{}).get('method')!='embedded_source_payload':
                continue
            fid=a.get('file_id','')
            if re.fullmatch(r'payload:(?:\d+|raw|text)',fid):
                suffix='service-event' if a.get('kind')=='service_event' else fid.split(':',1)[1]
                a['file_id']='payload:'+r['message_id']+':'+suffix
                changed+=1
        if r.get('completeness',{}).get('raw_archived') and isinstance(r.get('raw'),dict) and r['raw'].get('archived_payload')=='payload:raw':
            r['raw']['archived_payload']='payload:'+r['message_id']+':raw'
        if r.get('completeness',{}).get('text_archived'):
            r['text']=r['text'].replace('\n[Full text archived as payload:text]',
                                         '\n[Full text archived as payload:'+r['message_id']+':text]')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent,prefix='.capture-repair-') as tmp:
        folder=Path(tmp)/'package'
        folder.mkdir()
        shutil.copytree(directory/'blobs',folder/'blobs')
        parts=[]
        position=0
        for part in manifest['parts']:
            count=part['messages']
            data=canonical(records[position:position+count]).encode('utf-8')
            position+=count
            (folder/part['path']).write_bytes(data)
            parts.append(dict(part,size=len(data),sha256=hashlib.sha256(data).hexdigest()))
        Store(folder/'catalog').ingest(records,'capture-id-repair')
        updated=dict(manifest,parts=parts,created_at=now(),repaired_from=digest(canonical(manifest)),
                     repair='0.5.1-message-scoped-payload-ids')
        (folder/'manifest.json').write_text(canonical(updated),encoding='utf-8')
        folder.rename(destination)
    return {'directory':str(destination),'repaired_payloads':changed,'messages':len(records),
            'source_message_ids_preserved':True,'captured_bytes_preserved':True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('create', help='Snapshot selected normalized records and explicitly allowed attachments')
    p.add_argument('--records', type=Path, required=True, help='Selected normalized JSON array or JSONL')
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--source-root', type=Path, action='append', default=[])
    p.add_argument('--project-id')
    p.add_argument('--project-name')
    p.add_argument('--project-summary', default='')
    p.add_argument('--project-basis', choices=['user_selection', 'agent_inference'], default='user_selection')
    p = sub.add_parser('import', help='Resume importing a snapshot on the memory server')
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--home', type=Path, required=True)
    p.add_argument('--source-root', type=Path, action='append', required=True)
    args = parser.parse_args(argv)
    if args.command == 'create':
        text = args.records.read_text(encoding='utf-8-sig')
        records = json.loads(text) if text.lstrip().startswith('[') else [json.loads(line) for line in text.splitlines() if line.strip()]
        project = None
        if args.project_id or args.project_name:
            if not args.project_id:
                parser.error('project-id is required')
            project = dict(id=args.project_id, basis=args.project_basis, summary=args.project_summary)
            if args.project_name: project['name']=args.project_name
            else: project['existing']=True
        result = capture(records, args.destination, args.source_root, project)
    else:
        result = import_capture(Store(args.home), args.directory, args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
