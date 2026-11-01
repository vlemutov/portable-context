"""Consistent SQLite snapshots, portable ZIP recovery and optional private S3 delivery."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import uuid
import zipfile


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def create(home, directory):
    home, directory = Path(home).resolve(), Path(directory).resolve()
    if directory == home or directory.is_relative_to(home):
        raise ValueError('Backup directory must be outside the memory directory')
    if not (home / 'context.sqlite3').is_file():
        raise ValueError('Existing memory database required')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.zip'
    final = directory / name
    with tempfile.TemporaryDirectory(dir=directory, prefix='.snapshot-') as tmp:
        root = Path(tmp)
        db = root / 'context.sqlite3'
        source = sqlite3.connect((home / 'context.sqlite3').as_uri() + '?mode=ro', uri=True)
        dest = sqlite3.connect(db)
        try:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite snapshot failed integrity_check')
            originals = [r[0] for r in dest.execute('SELECT hash FROM imports')]
            workspace = dest.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()
        finally:
            dest.close()
            source.close()
        for digest in originals:
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid original identifier')
            src = home / 'originals' / digest
            if src.is_symlink() or sha256(src) != digest:
                raise ValueError('Missing or corrupt original: ' + digest)
            (root / 'originals').mkdir(exist_ok=True)
            shutil.copyfile(src, root / 'originals' / digest)
        # Manual Markdown is valuable but cannot be transactionally snapshotted with SQLite.
        # Preserve it separately: restore never silently overwrites canonical wiki from these files.
        vault = home / 'vault'
        if vault.exists():
            for src in vault.rglob('*'):
                if src.is_symlink():
                    raise ValueError('Symlinks are not supported in backup vaults')
                if src.is_file() and not src.name.startswith('.pc-'):
                    target = root / 'recovered-vault' / src.relative_to(vault)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, target)
        files = {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob('*') if p.is_file()}
        manifest = {'format': 1, 'workspace_id': workspace[0] if workspace else None,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'files': files,
                    'markdown': 'Non-transactional recovery copy; reconcile recovered-vault after restore.',
                    'excluded': ['External source files', 'Producer outboxes', 'Deployment state and credentials']}
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        temporary_zip = directory / (name + '.partial')
        try:
            with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
                for p in sorted(root.rglob('*')):
                    if p.is_file():
                        archive.write(p, p.relative_to(root).as_posix())
            os.chmod(temporary_zip, 0o600)
            os.replace(temporary_zip, final)
        finally:
            temporary_zip.unlink(missing_ok=True)
    return {'path': str(final), 'sha256': sha256(final), 'workspace_id': manifest['workspace_id']}


def restore(archive_path, destination, max_bytes=20 * 1024**3):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Restore requires a new destination; existing memories are never overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive, tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > max_bytes or len(infos) > 200000:
            raise ValueError('Archive exceeds restore limits')
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive paths')
        for info in infos:
            p = PurePosixPath(info.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in info.filename or ':' in info.filename
                    or info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe archive path')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('format') != 1 or set(names) != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('Unsupported or incomplete backup manifest')
        if 'context.sqlite3' not in manifest['files']:
            raise ValueError('Backup database missing')
        root = Path(tmp) / 'memory'
        root.mkdir(mode=0o700)
        for name, expected in manifest['files'].items():
            target = root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            if sha256(target) != expected:
                raise ValueError('Checksum mismatch: ' + name)
        with closing(sqlite3.connect(root / 'context.sqlite3')) as con:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Restored database failed integrity_check')
        # Rebuild canonical projections only; keep uncommitted disk edits for explicit reconciliation.
        from .core import Store
        from .workspace import Workspace
        from .wiki import project_wiki
        store = Store(root)
        workspace = Workspace(store)
        legacy = root / 'recovered-vault/memory'
        if legacy.is_dir():
            shutil.copytree(legacy, root / 'vault/memory', dirs_exist_ok=True)
        project_wiki(workspace)
        store.project()
        os.rename(root, destination)
    return {'path': str(destination), 'workspace_id': manifest['workspace_id'],
            'next': 'Review recovered-vault for manual edits and legacy notes; rebind local source paths before resuming imports.'}


def s3_client(config):
    from urllib.parse import urlparse
    if urlparse(config['endpoint']).scheme != 'https':
        raise ValueError('S3 endpoint must use HTTPS')
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=config['endpoint'], region_name=config['region'],
                        aws_access_key_id=config['access_key'], aws_secret_access_key=config['secret_key'],
                        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                                      connect_timeout=15, read_timeout=60, retries={'max_attempts': 4}))


def upload(path, config):
    path = Path(path)
    object_key = 'backups/' + path.name
    client = s3_client(config)
    checksum = sha256(path)
    client.upload_file(str(path), config['bucket'], object_key, ExtraArgs={'Metadata': {'sha256': checksum}})
    head = client.head_object(Bucket=config['bucket'], Key=object_key)
    if head['ContentLength'] != path.stat().st_size or head.get('Metadata', {}).get('sha256') != checksum:
        raise ValueError('S3 upload verification failed')
    return {'bucket': config['bucket'], 'key': object_key, 'sha256': checksum}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('create')
    p.add_argument('--home', required=True)
    p.add_argument('--directory', required=True)
    p.add_argument('--s3-config', type=Path)
    p = sub.add_parser('restore')
    p.add_argument('archive')
    p.add_argument('--destination', required=True)
    p = sub.add_parser('download')
    p.add_argument('--s3-config', type=Path, required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'create':
        result = create(args.home, args.directory)
        if args.s3_config:
            result['s3'] = upload(result['path'], json.loads(args.s3_config.read_text()))
    elif args.command == 'restore':
        result = restore(args.archive, args.destination)
    else:
        if args.output.exists():
            raise ValueError('Output already exists')
        config = json.loads(args.s3_config.read_text())
        client = s3_client(config)
        head = client.head_object(Bucket=config['bucket'], Key=args.key)
        expected = head.get('Metadata', {}).get('sha256')
        if not expected:
            raise ValueError('Backup checksum metadata missing')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
            part = Path(tmp) / 'backup.zip'
            client.download_file(config['bucket'], args.key, str(part))
            if sha256(part) != expected:
                raise ValueError('Downloaded backup checksum mismatch')
            os.chmod(part, 0o600)
            os.rename(part, args.output)
        result = {'path': str(args.output), 'sha256': expected}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
