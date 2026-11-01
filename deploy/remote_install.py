"""Runs on a user-owned Linux machine, over SSH. Input is a private JSON bundle on stdin."""
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import zipfile


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.new')
    tmp.write_text(value, encoding='utf-8')
    tmp.chmod(mode)
    tmp.replace(path)


def install(payload):
    if sys.platform != 'linux' or sys.version_info < (3, 11):
        raise ValueError('Automatic install requires Linux and Python 3.11+. See the server guide.')
    os.umask(0o077)
    root = Path.home() / '.local/share/portable-context-server'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    install_lock = (root / 'install.lock').open('w')
    fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root / 'bin/backup').exists():
        # Preserve a recoverable version before an upgrade can migrate the database.
        run([str(root / 'bin/backup')], stdout=sys.stderr)
    # Never replace a running release in-place. A failed install leaves current working.
    data = base64.b64decode(payload['bundle'], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    if digest != payload['sha256']:
        raise ValueError('Install bundle checksum mismatch')
    release = root / 'releases' / digest
    ready = release / '.ready'
    if not ready.exists():
        release.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                p = Path(info.filename)
                if p.is_absolute() or '..' in p.parts or '\\' in info.filename or ':' in info.filename:
                    raise ValueError('Invalid install bundle path')
                target = release / 'source' / p
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        venv = release / 'venv'
        run([sys.executable, '-m', 'venv', str(venv)], stdout=sys.stderr)
        python = str(venv / 'bin/python')
        run([python, '-m', 'pip', 'install', str(release / 'source') + '[mcp,backup]',
             'mcp==2.3.0'], stdout=sys.stderr)
        run([python, '-c', 'import sqlite3; c=sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE t USING fts5(x)"); import mcp, boto3'], stdout=sys.stderr)
        ready.touch()
    python = str(release / 'venv/bin/python')
    memory = root / 'memory'
    imports = root / 'imports'
    imports.mkdir(exist_ok=True)
    # Initializes only the database; never imports personal data as part of setup.
    run([python, '-m', 'portable_context', '--home', str(memory), 'init'], stdout=sys.stderr)
    backup = payload.get('backup')
    if backup:
        write(root / 'backup-config.json', json.dumps(backup))
    current = root / 'current'
    new = root / 'current.new'
    new.unlink(missing_ok=True)
    new.symlink_to(release, target_is_directory=True)
    new.replace(current)
    bin_dir = root / 'bin'
    cmd = [str(current / 'venv/bin/python'), '-m', 'portable_context', '--home', str(memory)]
    write(bin_dir / 'mcp', '#!/bin/sh\nexec ' + shlex.join(cmd + ['mcp', '--source-root', str(imports)]) + '\n', 0o700)
    write(bin_dir / 'status', '#!/bin/sh\nexec ' + shlex.join(cmd + ['status']) + '\n', 0o700)
    # One backup at a time; log contains status/errors, never S3 credentials.
    backup_cmd = [str(current / 'venv/bin/python'), '-m', 'portable_context.backup', 'create',
                  '--home', str(memory), '--directory', str(root / 'backups')]
    if (root / 'backup-config.json').exists():
        backup_cmd += ['--s3-config', str(root / 'backup-config.json')]
    body = '#!/bin/sh\nset -eu\numask 077\nexec 9>' + shlex.quote(str(root / 'backup.lock'))
    body += '\nflock -w 30 9 || exit 1\n' + shlex.join(backup_cmd) + '\n'
    # Retain only the newest 7 local archives after successful S3 delivery. Remote archives are retained.
    if (root / 'backup-config.json').exists():
        cleanup = ('from pathlib import Path; p=Path(' + repr(str(root / 'backups')) + '); '
                   '[f.unlink() for f in sorted(p.glob("*.zip"), key=lambda f:f.name)[:-7]]')
        body += shlex.join([str(current / 'venv/bin/python'), '-c', cleanup]) + '\n'
    write(bin_dir / 'backup', body, 0o700)
    # Verify a real upload, download and restore before reporting ready or scheduling backups.
    run([str(bin_dir / 'backup')], stdout=sys.stderr)
    archives = sorted((root / 'backups').glob('*.zip'))
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        archive = archives[-1]
        if (root / 'backup-config.json').exists():
            archive = Path(tmp) / 'download.zip'
            run([python, '-m', 'portable_context.backup', 'download', '--s3-config', str(root / 'backup-config.json'),
                 '--key', 'backups/' + archives[-1].name, '--output', str(archive)], stdout=sys.stderr)
        run([python, '-m', 'portable_context.backup', 'restore', str(archive),
             '--destination', str(Path(tmp) / 'restore')], stdout=sys.stderr)
    timer = 'not configured'
    if payload.get('schedule_backup', True):
        previous = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if previous.returncode not in (0, 1):
            raise ValueError('Cannot read crontab; schedule backups manually')
        if previous.returncode == 1 and (previous.stdout.strip() or 'no crontab for' not in previous.stderr.lower()):
            raise ValueError('Unexpected crontab result; existing entries were not changed')
        marker = '# portable-context-managed-backup'
        lines = [line for line in previous.stdout.splitlines() if not line.endswith(marker)]
        # Cron treats % specially even within quotes; reject unusual homes instead of breaking the job.
        if '%' in str(root) or '\n' in str(root):
            raise ValueError('Home directory is incompatible with this cron installer')
        # Replace the single status log per run; it cannot grow indefinitely.
        lines.append('17 3 * * * ' + shlex.quote(str(bin_dir / 'backup')) + ' >'
                     + shlex.quote(str(root / 'backup.log')) + ' 2>&1 ' + marker)
        run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True, stdout=sys.stderr)
        timer = 'daily 03:17 server time; cron daemon must be running'
    status = subprocess.check_output([str(bin_dir / 'status')], text=True)
    return {'ready': True, 'root': str(root), 'release': digest, 'backup': 'S3 verified' if (root / 'backup-config.json').exists() else 'local',
            'schedule': timer, 'memory': json.loads(status)}


if __name__ == '__main__':
    try:
        print(json.dumps(install(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        # Do not serialize payload/credentials in an exception report.
        print('Server installation failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
