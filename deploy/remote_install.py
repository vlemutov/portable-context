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
import time
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
        if (root/'restic-config.json').exists():
            run([str(root / 'bin/backup')], stdout=sys.stderr)
        else:
            # The legacy wrapper also deletes old local ZIPs. Migration must not run that cleanup
            # before the first verified restic restore; create the extra ZIP directly instead.
            legacy_lock=(root/'backup.lock').open('a')
            fcntl.flock(legacy_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            command=[str(root/'current/venv/bin/python'),'-m','portable_context.backup','create',
                '--home',str(root/'memory'),'--directory',str(root/'backups')]
            if (root/'backup-config.json').exists(): command+=['--s3-config',str(root/'backup-config.json')]
            run(command,stdout=sys.stderr)
            legacy_lock.close()
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
    backup = payload.get('backup')
    if backup:
        write(root / 'backup-config.json', json.dumps(backup))
    password_path=root/'restic-password'
    if password_path.exists() and password_path.read_text().strip()!=payload['recovery_password'].strip():
        raise ValueError('Recovery password differs; restore the original private deployment state before upgrading')
    write(password_path,payload['recovery_password'].strip()+'\n')
    tool_namespace={'__name__':'restic_installer'}
    exec(payload['restic_installer'],tool_namespace)
    binary=tool_namespace['install'](root/'tools')
    config_path=root/'restic-config.json'
    existing_config=config_path.exists()
    if not (memory/'context.sqlite3').exists():
        run([python,'-m','portable_context','--home',str(memory),'init'],stdout=sys.stderr)
    import sqlite3
    with sqlite3.connect(memory/'context.sqlite3') as db:
        workspace_id=db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    repository=payload.get('backup_repository') or (('s3:'+backup['endpoint'].rstrip('/')+'/'+backup['bucket']+'/restic/'+workspace_id)
        if backup else str(root/'restic-repository'))
    if existing_config:
        prior=json.loads(config_path.read_text())
        if prior['repository']!=repository: raise ValueError('Backup repository changed; use a separate reviewed migration')
    config={'binary':binary,'repository':repository,'password_file':str(password_path),
        'home':str(memory),'state':str(root/'backup-state')}
    if backup: config['s3_config']=str(root/'backup-config.json')
    write(config_path,json.dumps(config))
    restic_cmd=[python,'-m','portable_context.restic_backup','--home',str(memory),'--config',str(config_path)]
    # Retry an interrupted initial setup by opening first. A failed init never overwrites a repository.
    opened=subprocess.run(restic_cmd+['list'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if opened.returncode:
        run(restic_cmd+['init'],stdout=sys.stderr)
        run(restic_cmd+['list'],stdout=sys.stderr)
    # Block reconnects before migration; keep the gate closed on failure instead of allowing old writers.
    maintenance=root/'maintenance.json'
    write(maintenance,json.dumps({'operation':'upgrade','release':digest}))
    attachment_env=('export PC_ATTACHMENT_S3_CONFIG='+shlex.quote(str(root/'backup-config.json'))+'\n' if backup else '')
    current=root/'current'
    cmd=[str(current/'venv/bin/python'),'-m','portable_context','--home',str(memory)]
    gate='if [ -e '+shlex.quote(str(maintenance))+' ]; then echo "Memory maintenance in progress; retry after installer succeeds" >&2; exit 75; fi; '
    wrapper='#!/bin/sh\n'+attachment_env+'exec flock -s -n '+shlex.quote(str(root/'service.lock'))+' sh -c '+shlex.quote(gate+'exec '+shlex.join(cmd+['mcp','--source-root',str(imports)]))+'\n'
    write(root/'bin/mcp',wrapper,0o700)
    stop_script = "from portable_context.maintenance import active_mcp; from pathlib import Path; import os,signal,time; r=Path.home()/'.local/share/portable-context-server'; [os.kill(p,signal.SIGTERM) for p in active_mcp(r)]; time.sleep(1); assert not active_mcp(r), 'MCP process still active; close it and rerun install'"
    run([python,'-c',stop_script],stdout=sys.stderr)
    service_lock=(root/'service.lock').open('a')
    fcntl.flock(service_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    # The new Store performs a restic checkpoint before a legacy schema migration.
    run([python,'-m','portable_context','--home',str(memory),'init'],stdout=sys.stderr)
    run(restic_cmd+['backup'],stdout=sys.stderr)
    # Validate a complete self-contained restoration before changing the active release or schedule.
    verify_script = "from portable_context.restic_backup import Manager; from pathlib import Path; import tempfile; r=Path.home()/'.local/share/portable-context-server'; m=Manager(r/'memory',r/'restic-config.json'); s=m.snapshots()[-1]['id']; t=tempfile.TemporaryDirectory(dir=r); m.restore(s,Path(t.name)/'restore'); t.cleanup()"
    run([python,'-c',verify_script],stdout=sys.stderr)
    if current.exists() and current.resolve()!=release:
        previous=root/'previous'; previous.unlink(missing_ok=True); previous.symlink_to(current.resolve(),target_is_directory=True)
    new=root/'current.new'; new.unlink(missing_ok=True); new.symlink_to(release,target_is_directory=True); new.replace(current)
    bin_dir=root/'bin'
    cmd=[str(current/'venv/bin/python'),'-m','portable_context','--home',str(memory)]
    write(bin_dir/'mcp',wrapper,0o700)
    write(bin_dir/'status','#!/bin/sh\n'+attachment_env+'exec '+shlex.join(cmd+['status'])+'\n',0o700)
    restic_cmd[0]=str(current/'venv/bin/python')
    for name,command in (('backup','backup'),('backup-check','check'),('backup-prune','prune')):
        write(bin_dir/name,'#!/bin/sh\nset -eu\numask 077\n'+shlex.join(restic_cmd+[command])+'\n',0o700)
    timer = 'not configured'
    if payload.get('schedule_backup', True):
        previous = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if previous.returncode not in (0, 1):
            raise ValueError('Cannot read crontab; schedule backups manually')
        if previous.returncode == 1 and (previous.stdout.strip() or 'no crontab for' not in previous.stderr.lower()):
            raise ValueError('Unexpected crontab result; existing entries were not changed')
        marker = '# portable-context-managed-backup'
        markers=(marker,'# portable-context-managed-check','# portable-context-managed-prune')
        lines = [line for line in previous.stdout.splitlines() if not line.endswith(markers)]
        # Cron treats % specially even within quotes; reject unusual homes instead of breaking the job.
        if '%' in str(root) or '\n' in str(root):
            raise ValueError('Home directory is incompatible with this cron installer')
        # Replace the single status log per run; it cannot grow indefinitely.
        lines.append('17 3 * * * ' + shlex.quote(str(bin_dir / 'backup')) + ' >'
                     + shlex.quote(str(root / 'backup.log')) + ' 2>&1 ' + marker)
        lines.append('47 3 * * 0 '+shlex.quote(str(bin_dir/'backup-prune'))+' >'+shlex.quote(str(root/'backup-prune.log'))+' 2>&1 # portable-context-managed-prune')
        lines.append('17 4 1 * * '+shlex.quote(str(bin_dir/'backup-check'))+' >'+shlex.quote(str(root/'backup-check.log'))+' 2>&1 # portable-context-managed-check')
        run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True, stdout=sys.stderr)
        timer = 'daily 03:17 server time; cron daemon must be running'
    status = subprocess.check_output([str(bin_dir / 'status')], text=True)
    maintenance.unlink()
    service_lock.close()
    return {'ready': True, 'root': str(root), 'release': digest, 'backup': 'restic S3 verified' if (root / 'backup-config.json').exists() else 'restic local verified',
            'schedule': timer, 'memory': json.loads(status)}


if __name__ == '__main__':
    try:
        print(json.dumps(install(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        # Do not serialize payload/credentials in an exception report.
        print('Server installation failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
