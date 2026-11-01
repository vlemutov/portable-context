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

from .core import Store, canonical, digest, key, now, validate
from .catalog import Catalog, project_catalog


def capture(records, destination, allowed_roots=(), memory_project=None, max_file_bytes=512 * 1024 * 1024):
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError('Capture destination already exists; keep immutable export snapshots separate')
    roots = [Path(p).expanduser().resolve() for p in allowed_roots]
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {'stored_occurrences': 0, 'missing_occurrences': 0, 'unique_bytes': 0, 'unique_blobs': 0}
    with tempfile.TemporaryDirectory(prefix='.capture-', dir=destination.parent) as tmp:
        folder = Path(tmp) / 'package'
        (folder / 'blobs').mkdir(parents=True)
        normalized = []
        for original in records:
            r = validate(json.loads(canonical(original)))
            if memory_project is not None:
                r['memory_project'] = memory_project
            r = validate(r)
            attachments = []
            for original_a in r['attachments']:
                a = dict(original_a) if isinstance(original_a, dict) else {'reference': original_a}
                a.setdefault('name', a.get('file_name') or str(a.get('path') or a.get('url') or 'attachment').replace('\\', '/').rsplit('/', 1)[-1])
                a.setdefault('role', 'unknown')
                a.setdefault('purpose', 'unknown')
                a.setdefault('status', 'unknown')
                a['export_observation'] = {'captured_at': now(), 'method': 'portable-context-capture'}
                # A local path is read only if explicitly authorized by root, never by historical instructions.
                source = Path(a['path']).expanduser().resolve() if isinstance(a.get('path'), str) else None
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
                            a['export_observation'].update(blob='blobs/' + sha, source_modified_ns=after.st_mtime_ns)
                            report['stored_occurrences'] += 1
                            attachments.append(a)
                            continue
                elif source:
                    reason = 'outside_allowed_roots'
                a['availability'] = 'unavailable'
                a['export_observation']['reason'] = reason
                report['missing_occurrences'] += 1
                attachments.append(a)
            r['attachments'] = attachments
            normalized.append(r)
        # Passports and entry pages exist inside the capture itself, before remote import.
        draft = Store(folder / 'catalog')
        draft.ingest(normalized, 'client-capture')
        data = canonical(normalized)
        (folder / 'records.json').write_text(data, encoding='utf-8')
        manifest = {'format': 'portable-context-capture-1', 'created_at': now(), 'records_sha256': digest(data),
                    'messages': len(normalized), **report,
                    'note': 'These are currently accessible file bytes; original message-time bytes cannot be inferred from a mutable path.'}
        (folder / 'manifest.json').write_text(canonical(manifest), encoding='utf-8')
        # The draft catalog contains reference passports; remote transfer binds captured hashes.
        folder.rename(destination)
    return {'directory': str(destination), **manifest}


def import_capture(store, directory, allowed_roots):
    directory = Path(directory).expanduser().resolve()
    roots = [Path(p).expanduser().resolve() for p in allowed_roots]
    if not any(directory.is_relative_to(root) for root in roots):
        raise ValueError('Capture must be under an allowed server source root')
    manifest_path, records_path = directory / 'manifest.json', directory / 'records.json'
    for path in (manifest_path, records_path):
        if not path.resolve().is_relative_to(directory) or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Unsafe or oversized capture manifest/records')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 'portable-context-capture-1':
        raise ValueError('Unsupported capture format')
    data = records_path.read_text(encoding='utf-8')
    if digest(data) != manifest['records_sha256']:
        raise ValueError('Capture record checksum mismatch')
    records = [validate(r) for r in json.loads(data)]
    # A bundle may reference ONLY its own hashed blobs, never arbitrary source paths.
    transfers = []
    for r in records:
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
    totals = {'inserted': 0, 'revised': 0, 'unchanged': 0}
    for start in range(0, len(records), 100):
        with store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            result = store._ingest(con, records[start:start + 100], 'capture:' + manifest['records_sha256'])
        for k in totals:
            totals[k] += result[k]
    catalog = Catalog(store, [directory])
    receipts = [catalog.store_attachment(oid, server_path=str(path), expected_sha256=sha) for oid, path, sha in transfers]
    project_catalog(store)
    return {'messages': totals, 'attachments': receipts, 'missing_at_capture': manifest.get('missing_occurrences'),
            'note': 'Safe to resume the same immutable package; inspect each S3 status separately.'}


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
            if not args.project_id or not args.project_name:
                parser.error('Both project-id and project-name are required')
            project = dict(id=args.project_id, name=args.project_name, basis=args.project_basis, summary=args.project_summary)
        result = capture(records, args.destination, args.source_root, project)
    else:
        result = import_capture(Store(args.home), args.directory, args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
