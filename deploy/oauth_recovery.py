"""Owner-operated OAuth backups and recovery. Never restore old credentials live.

Run with the server's Python environment. All output is a receipt, never a key.
Recovery prepares a separate identity runtime; activation explicitly switches bound services.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
import urllib.request
from urllib.parse import urlsplit

from pocketid import Bootstrap, VERSION, LINUX_AMD64_SHA256
from setup import private_directory
from portable_context.core import atomic_write, canonical, now
from portable_context.oauth import OAuthConfig
from portable_context.restic_backup import Manager, file_lock

FORMAT = 'portable-context-identity-1'
TAG = 'portable-context-identity'


def provider_environment(values):
    # Inherited DB/key variables must not redirect a fresh provider to a live DB.
    ambient = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP', 'LANG') if k in os.environ}
    return {**ambient, **values}


def repository_key(value):
    if value.startswith('s3:'): return value.rstrip('/')
    return os.path.normcase(str(Path(value).resolve())).replace('\\', '/').rstrip('/')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    atomic_write(Path(path), canonical(value))
    Path(path).chmod(0o600)


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Expected a regular file within the private runtime')
    cursor = path.parent
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('Linked directories are not supported')
        if cursor == cursor.parent:
            raise ValueError('File outside private runtime')
        cursor = cursor.parent
    return path


def runtime_info(runtime):
    runtime = Path(runtime).resolve()
    auth = runtime/'identity'
    env = read(regular(auth/'environment.json', runtime))
    receipt = read(regular(auth/'receipt.json', runtime))
    config = OAuthConfig.load(regular(runtime/'oauth.json', runtime))
    if receipt['owner_subject'] != config.owner_subject or receipt['origin'] != config.issuer or env['APP_URL'] != config.issuer:
        raise ValueError('Owner, issuer and bootstrap receipt must agree')
    # Current reproducible profile only. Do not silently omit external stores.
    required = {'HOST': '127.0.0.1', 'UI_CONFIG_DISABLED': 'true', 'ALLOW_USER_SIGNUPS': 'disabled'}
    if any(env.get(k) != v for k, v in required.items()):
        raise ValueError('Expected private no-signup identity profile')
    defaults = {'DB_CONNECTION_STRING': 'data/pocket-id.db', 'FILE_BACKEND': 'filesystem',
                'UPLOAD_PATH': 'data/uploads'}
    if any(env.get(k, v) != v for k, v in defaults.items()):
        raise ValueError('Recovery supports the local SQLite/filesystem profile only')
    if any(k.startswith(('FRANCIS_', 'EXPERIMENTAL_FRANCIS_', 'TLS_', 'SIGNING_', 'S3_')) or k.endswith('_FILE') for k in env):
        raise ValueError('External runtime or key files require a separate backup profile')
    if checksum(regular(auth/'pocket-id', runtime)) != LINUX_AMD64_SHA256:
        raise ValueError('Identity binary is not the pinned Pocket ID version')
    return runtime, env, receipt, config


def configure(runtime, memory_config, config_path, repository):
    runtime, _, _, oauth = runtime_info(runtime)
    base = read(memory_config)
    if not repository or repository_key(repository) == repository_key(base['repository']):
        raise ValueError('OAuth requires a separate restic repository')
    # Avoid nesting repositories as well as identical paths.
    a, b = repository_key(repository), repository_key(base['repository'])
    if a.startswith(b+'/') or b.startswith(a+'/'):
        raise ValueError('Use a sibling repository, not a nested repository')
    path = Path(config_path).resolve()
    state = private_directory(path.parent/'identity-backup-state')
    if state == runtime or state.is_relative_to(runtime):
        raise ValueError('Backup state must be outside the identity runtime')
    result = {k: base[k] for k in ('binary', 'password_file', 's3_config') if k in base}
    result.update(repository=repository, state=str(state), runtime=str(runtime),
                  memory_home=base['home'], memory_repository=base['repository'], issuer=oauth.issuer, format=FORMAT)
    private_directory(path.parent)
    if path.exists():
        previous = read(path)
        previous.setdefault('memory_repository', base['repository'])
        if previous != result:
            raise ValueError('Existing identity backup configuration differs; review migration')
    write(path, result)
    return {'configured': True, 'issuer': oauth.issuer, 'initialized': False,
            'next': 'Initialize the distinct repository once, then run backup and check.'}


class IdentityBackups:
    def __init__(self, config_path):
        self.path = Path(config_path).resolve()
        self.config = read(self.path)
        if self.config.get('format') != FORMAT:
            raise ValueError('Unsupported identity backup configuration')
        self.state = private_directory(self.config['state'])
        self.runtime = Path(self.config['runtime']).resolve()
        if self.state == self.runtime or self.state.is_relative_to(self.runtime):
            raise ValueError('Backup state overlaps identity runtime')
        # Reuse the redacted restic process runner, with an independent repository/state.
        self.runner = Manager(self.config['memory_home'], self.path)

    def initialize(self):
        self.runner.initialize()  # Explicit command; no init fallback on a network error.
        return {'initialized': True, 'kind': TAG}

    def snapshots(self):
        rows = json.loads(self.runner.run(['snapshots'])) or []
        if any(TAG not in row.get('tags', []) for row in rows):
            raise ValueError('Identity repository contains unrelated snapshots; refusing operation')
        return rows

    def status(self):
        p = self.state/'status.json'
        return {'configured': True, 'kind': TAG, 'issuer': self.config['issuer'],
                'last_backup': read(p) if p.exists() else None,
                'last_restore_check': read(self.state/'check.json') if (self.state/'check.json').exists() else None,
                'last_failure': read(self.state/'failure.json') if (self.state/'failure.json').exists() else None,
                'recovery_policy': 'fresh identity, fresh passkeys, fresh application consent; old credentials never activated'}

    def backup(self):
        with file_lock(self.state/'operation.lock'):
            runtime, env, receipt, oauth = runtime_info(self.runtime)
            if oauth.issuer != self.config['issuer']:
                raise ValueError('Backup issuer changed; review migration')
            with tempfile.TemporaryDirectory(prefix='identity-stage-', dir=self.state) as tmp:
                stage = private_directory(tmp)
                originals = {'environment.json': runtime/'identity/environment.json',
                             'receipt.json': runtime/'identity/receipt.json', 'oauth.json': runtime/'oauth.json'}
                for name, path in originals.items():
                    shutil.copyfile(regular(path, runtime), stage/name)
                    (stage/name).chmod(0o600)
                db = regular(runtime/'identity/data/pocket-id.db', runtime)
                with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(stage/'pocket-id.db')) as dst:
                    src.backup(dst)
                    dst.execute('PRAGMA journal_mode=DELETE')
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Identity SQLite snapshot failed integrity check')
                (stage/'pocket-id.db').chmod(0o600)
                data = runtime/'identity/data'
                for path in data.rglob('*'):
                    if path.is_symlink(): raise ValueError('Identity data contains a symlink')
                    if not path.is_file() or path.name in ('pocket-id.db', 'pocket-id.db-wal', 'pocket-id.db-shm'):
                        continue
                    regular(path, runtime)
                    target = stage/'data-files'/path.relative_to(data)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(path, target); target.chmod(0o600)
                if any((stage/name).read_bytes() != path.read_bytes() for name, path in originals.items()):
                    raise ValueError('Identity configuration changed during backup; retry')
                files = {p.relative_to(stage).as_posix(): checksum(p) for p in stage.rglob('*') if p.is_file()}
                manifest = {'format': FORMAT, 'provider_version': VERSION, 'issuer': oauth.issuer,
                            'owner_subject': receipt['owner_subject'], 'files': files,
                            'restore_policy': 'quarantine-only; rebuild authentication from fresh state'}
                fingerprint = hashlib.sha256(canonical(manifest).encode()).hexdigest()
                write(stage/'manifest.json', manifest)
                previous = next((s for s in self.snapshots() if 'content:'+fingerprint in s.get('tags', [])), None)
                if previous:
                    sid, state = previous['id'], 'unchanged'
                else:
                    output = self.runner.run(['backup', '.', '--tag', TAG, '--tag', 'content:'+fingerprint], cwd=stage)
                    summary = next(json.loads(line) for line in output.splitlines() if json.loads(line).get('message_type') == 'summary')
                    sid, state = summary['snapshot_id'], 'created'
                result = {'at': now(), 'status': state, 'snapshot_id': sid, 'files': len(files), 'kind': TAG}
                write(self.state/'status.json', result)
                return result

    def restore(self, snapshot, target):
        """Decrypt into quarantine only. Never copy this tree to a running provider."""
        if not re.fullmatch('[0-9a-f]{64}', snapshot): raise ValueError('Use a full snapshot ID')
        target = Path(target).resolve()
        forbidden = [self.runtime, self.state, Path(self.config['memory_home']).resolve()]
        if target.exists() or any(target == p or target.is_relative_to(p) or p.is_relative_to(target) for p in forbidden):
            raise ValueError('Restore requires a new directory outside live runtime, memory and backup state')
        with file_lock(self.state/'operation.lock'):
            if not any(s['id'] == snapshot for s in self.snapshots()):
                raise ValueError('Snapshot not found in identity repository')
            private_directory(target)
            try:
                self.runner.run(['restore', snapshot, '--target', str(target)])
                manifest = verify_quarantine(target, self.config['issuer'])
                result = {'verified': True, 'snapshot_id': snapshot, 'files': len(manifest['files']),
                          'activated': False, 'target': str(target), 'at': now()}
                write(self.state/'check.json', result)
                return result
            except Exception:
                # Leave diagnostic state private and visibly unusable after an interrupted restore.
                write(target/'RESTORE-FAILED.json', {'usable': False})
                raise

    def check(self):
        rows = self.snapshots()
        if not rows: raise ValueError('No identity backup to check')
        latest = max(rows, key=lambda s: s['time'])
        with tempfile.TemporaryDirectory(prefix='identity-check-', dir=self.state.parent) as tmp:
            result = self.restore(latest['id'], Path(tmp)/'quarantine')
        result.pop('target', None)
        write(self.state/'check.json', result)
        return result

    def prune(self):
        self.snapshots()  # Refuse a mixed repository before any retention operation.
        return self.runner.prune()


def verify_quarantine(root, expected_origin):
    root = Path(root).resolve()
    if (root/'RESTORE-FAILED.json').exists(): raise ValueError('Incomplete restored archive')
    manifest = read(regular(root/'manifest.json', root))
    if manifest.get('format') != FORMAT or manifest.get('provider_version') != VERSION or manifest.get('issuer') != expected_origin:
        raise ValueError('Recovery archive version or issuer mismatch')
    files = manifest.get('files', {})
    if not {'pocket-id.db', 'environment.json', 'receipt.json', 'oauth.json'} <= files.keys():
        raise ValueError('Incomplete identity archive')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Unexpected files in identity archive')
    for rel, digest in files.items():
        if '\\' in rel or ':' in rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise ValueError('Unsafe archive path')
        if checksum(regular(root/rel, root)) != digest:
            raise ValueError('Identity archive checksum mismatch')
    with closing(sqlite3.connect((root/'pocket-id.db').as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Identity database failed integrity check')
    oauth = OAuthConfig.load(root/'oauth.json')
    if oauth.issuer != expected_origin or oauth.owner_subject != manifest['owner_subject'] or read(root/'receipt.json')['owner_subject'] != oauth.owner_subject:
        raise ValueError('Identity archive binding mismatch')
    return manifest


def prepare_recovery(quarantine, target, binary, expected_origin, port=14120):
    """Build a fresh stopped identity. Archived DB/keys/passkeys are NEVER copied."""
    manifest = verify_quarantine(quarantine, expected_origin)
    target = Path(target).resolve()
    source = Path(quarantine).resolve()
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('Recovery requires a separate new runtime directory')
    if checksum(binary) != LINUX_AMD64_SHA256:
        raise ValueError('Recovery binary checksum does not match pinned provider')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid recovery loopback port')
    private_directory(target)
    auth = private_directory(target/'identity')
    shutil.copyfile(binary, auth/'pocket-id'); (auth/'pocket-id').chmod(0o700)
    # Deliberately do not carry environment overrides or any historical permissions forward.
    env = {'APP_URL': expected_origin, 'HOST': '127.0.0.1', 'PORT': str(port),
           'ENCRYPTION_KEY': secrets.token_urlsafe(48), 'STATIC_API_KEY': secrets.token_urlsafe(48),
           'UI_CONFIG_DISABLED': 'true', 'ALLOW_USER_SIGNUPS': 'disabled', 'REQUIRE_USER_EMAIL': 'false',
           'ANALYTICS_DISABLED': 'true', 'VERSION_CHECK_DISABLED': 'true', 'ICON_LIBRARY_URL': 'disabled',
           'APP_NAME': 'Portable Context', 'LOG_LEVEL': 'error', 'LOG_QUERY_ARGS': 'false', 'CIMD_URL_ALLOWLIST': '[]',
           'ACTORS_HOST': '127.0.0.1', 'ACTORS_PORT': str(port+1 if port<65535 else port-1)}
    write(auth/'environment.json', env)
    (auth/'run.py').write_text("import os,json,pathlib\nr=pathlib.Path(__file__).parent\nos.chdir(r)\ne={k:os.environ[k] for k in ('PATH','HOME','USER','LOGNAME','SYSTEMROOT','WINDIR','TMP','TEMP','LANG') if k in os.environ}\ne.update(json.loads((r/'environment.json').read_text()))\nos.execve(str(r/'pocket-id'),[str(r/'pocket-id')],e)\n", encoding='utf-8')
    (auth/'run.py').chmod(0o600)
    write(target/'recovery.json', {'format': FORMAT, 'issuer': expected_origin,
          'previous_owner_subject': manifest['owner_subject'], 'state': 'prepared',
          'credentials_restored': False, 'activation_allowed': False,
          'environment_sha256': checksum(auth/'environment.json')})
    return {'prepared': True, 'target': str(target), 'credentials_restored': False,
            'next': 'bootstrap-recovery privately, then review and activate with MCP stopped; enroll new passkey and reconnect clients'}


def bootstrap_recovery(target):
    """Initialize only the freshly prepared loopback candidate and stop it again."""
    target = Path(target).resolve()
    marker = read(target/'recovery.json')
    if marker.get('state') != 'prepared' or marker.get('credentials_restored') is not False:
        raise ValueError('Expected a fresh prepared recovery candidate')
    auth = target/'identity'; env = read(auth/'environment.json')
    if (auth/'data').exists() or (auth/'receipt.json').exists():
        raise ValueError('Candidate already started; use a new target after interruption')
    if (checksum(auth/'pocket-id') != LINUX_AMD64_SHA256 or checksum(auth/'environment.json') != marker['environment_sha256']
            or env['APP_URL'] != marker['issuer'] or env['HOST'] != '127.0.0.1'):
        raise ValueError('Recovery candidate configuration changed')
    port = int(env['PORT'])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
    process = subprocess.Popen([str(auth/'pocket-id')], cwd=auth, env=provider_environment(env),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        api = Bootstrap(port, env['STATIC_API_KEY'])
        for _ in range(100):
            if process.poll() is not None: raise ValueError('Fresh identity process failed to start')
            try:
                api.request('/api/users'); break
            except (OSError, RuntimeError): time.sleep(.1)
        else: raise ValueError('Fresh identity did not become ready')
        receipt = api.configure(marker['issuer'], auth/'receipt.json')
        if receipt['owner_subject'] == marker['previous_owner_subject']:
            raise ValueError('Recovery must create a new owner identity')
        credentials = api.request('/api/users/'+receipt['owner_subject']+'/webauthn-credentials') or []
        if credentials: raise ValueError('Fresh owner unexpectedly has old passkeys')
        write(target/'oauth.json', {'issuer': marker['issuer'], 'resource': marker['issuer']+'/mcp',
              'provider_origin': f'http://127.0.0.1:{port}', 'owner_subject': receipt['owner_subject']})
        marker.update(state='bootstrapped', new_owner_subject=receipt['owner_subject'],
                      activation_allowed=False, passkeys=0, requires_new_enrollment=True)
        write(target/'recovery.json', marker)
        return {'bootstrapped': True, 'old_credentials_accepted': False, 'passkeys': 0,
                'activated': False, 'requires_new_enrollment': True}
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()


def owner_login(runtime, output):
    runtime, env, receipt, config = runtime_info(runtime)
    output = Path(output).resolve()
    if output.exists(): raise ValueError('Use a new private output file')
    private_directory(output.parent)
    code = Bootstrap(int(env['PORT']), env['STATIC_API_KEY']).create_owner_login_code(receipt['owner_subject'])
    # Write the secret once, never return it to the agent or stdout.
    write(output, {'code': code, 'expires_in_minutes': 20,
                   'login_url': config.issuer+'/login/alternative/code?redirect=/settings/account'})
    return {'created': True, 'private_file': str(output), 'expires_in_minutes': 20,
            'next': 'Owner enters code, adds a passkey, removes lost keys and revokes lost-device clients; do not paste code in chat'}


def export_kit(config, output):
    """Offline secret kit. Its password must not be stored only beside the server."""
    manager = IdentityBackups(config)
    output = Path(output).resolve()
    if output.exists(): raise ValueError('Use a new private recovery-kit directory')
    if output.is_relative_to(manager.runtime) or output.is_relative_to(manager.state):
        raise ValueError('Recovery kit must be outside runtime and staging')
    private_directory(output)
    files = {'restic-password': manager.config['password_file']}
    if manager.config.get('s3_config'): files['s3-config.json'] = manager.config['s3_config']
    for name, path in files.items():
        shutil.copyfile(path, output/name); (output/name).chmod(0o600)
    memory = None
    db = Path(manager.config['memory_home'])/'context.sqlite3'
    if manager.config.get('memory_repository') and db.is_file():
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as con:
            workspace = con.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
        memory = {'repository': manager.config['memory_repository'], 'workspace_id': workspace}
    write(output/'kit.json', {'format': FORMAT, 'issuer': manager.config['issuer'],
          'repository': manager.config['repository'], 'provider_version': VERSION,
          'memory': memory,
          'files': {name: checksum(output/name) for name in files},
          'warning': 'Contains repository password and storage credentials. Keep private and outside the server.'})
    return {'created': True, 'private_directory': str(output), 'contains_secrets': True}


def restore_memory_config(kit, config, memory_home, binary, expected_origin):
    """Rebuild memory backup access using the same offline kit, without creating a repo."""
    kit = Path(kit).resolve()
    manifest = read(regular(kit/'kit.json', kit))
    memory = manifest.get('memory')
    if not memory or not memory.get('repository') or not memory.get('workspace_id'):
        raise ValueError('Kit has no memory repository metadata; use a current installer kit')
    uuid.UUID(memory['workspace_id'])
    # Shared validation of file allowlist, hashes and issuer; temporary config is never activated.
    with tempfile.TemporaryDirectory(prefix='kit-validate-') as tmp:
        proof = Path(tmp)/'config.json'
        restore_config(kit, proof, Path(tmp)/'unused', memory_home, binary, expected_origin)
        value = read(proof)
    config = Path(config).resolve()
    if config.exists(): raise ValueError('Use a new memory backup configuration')
    private_directory(config.parent)
    value = {k: value[k] for k in ('binary', 'password_file', 's3_config') if k in value}
    value.update(home=str(Path(memory_home).resolve()), repository=memory['repository'],
                 state=str(private_directory(config.parent/'backup-state')))
    write(config, value)
    return {'configured': True, 'workspace_id': memory['workspace_id'], 'initialize_repository': False}


def restore_config(kit, config, runtime, memory_home, binary, expected_origin):
    """Open existing encrypted backups after server loss; never initializes a repo."""
    kit = Path(kit).resolve(); manifest = read(regular(kit/'kit.json', kit))
    if manifest.get('format') != FORMAT or manifest.get('issuer') != expected_origin:
        raise ValueError('Recovery kit format or issuer mismatch')
    files = manifest.get('files', {})
    if set(files) not in ({'restic-password'}, {'restic-password', 's3-config.json'}):
        raise ValueError('Invalid recovery kit files')
    for name, digest in files.items():
        if checksum(regular(kit/name, kit)) != digest: raise ValueError('Recovery kit checksum mismatch')
    config = Path(config).resolve()
    if config.exists(): raise ValueError('Use a new backup configuration; existing configuration is never replaced')
    if not Path(binary).is_file(): raise ValueError('Supply the verified restic executable')
    private_directory(config.parent)
    state = private_directory(config.parent/'identity-backup-state')
    value = {'format': FORMAT, 'issuer': expected_origin, 'repository': manifest['repository'],
             'binary': str(Path(binary).resolve()), 'password_file': str(kit/'restic-password'),
             'state': str(state), 'runtime': str(Path(runtime).resolve()), 'memory_home': str(Path(memory_home).resolve())}
    if 's3-config.json' in files: value['s3_config'] = str(kit/'s3-config.json')
    if manifest.get('memory'): value['memory_repository'] = manifest['memory']['repository']
    write(config, value)
    return {'configured': True, 'initialize_repository': False, 'next': 'list, restore to quarantine, prepare-recovery'}


def cron_lines(previous, script, python, config, state):
    paths = [str(Path(p).resolve()) for p in (script, python, config, state)]
    if any(any(c in p for c in ('%', '\n', '\r')) for p in paths):
        raise ValueError('Paths incompatible with cron')
    script, python, config, state = paths
    markers = tuple('# portable-context-identity-'+x for x in ('backup', 'check', 'prune'))
    lines = [line for line in previous.splitlines() if not line.endswith(markers)]
    for command, schedule in [('backup', '27 */6 * * *'), ('check', '37 4 * * 1'), ('prune', '47 4 * * 0')]:
        args = shlex.join([python, script, command, '--config', config])
        lines.append(schedule+' '+args+' >'+shlex.quote(str(Path(state)/(command+'.log')))+' 2>&1 # portable-context-identity-'+command)
    return '\n'.join(lines)+'\n'


def install_schedule(config):
    import sys
    if sys.platform != 'linux': raise ValueError('Schedule on the Linux memory server; local CLI is cross-platform')
    manager = IdentityBackups(config)
    if not manager.snapshots(): raise ValueError('Verify the first backup before scheduling')
    previous = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if previous.returncode not in (0, 1) or (previous.returncode == 1 and 'no crontab for' not in previous.stderr.lower()):
        raise ValueError('Could not read existing crontab; no changes made')
    value = cron_lines(previous.stdout, __file__, sys.executable, config, manager.state)
    subprocess.run(['crontab', '-'], input=value, text=True, check=True, capture_output=True)
    result = {'backup': 'every 6 hours at minute 27', 'check': 'Monday 04:37',
              'prune': 'Sunday 04:47', 'timezone': 'server', 'retention': manager.runner.policy()['values']}
    write(manager.state/'schedule.json', result)
    return result


def activate_recovery(target, backup_config, expected_owner, confirmation, *,
                      identity_service='pc-identity.service', mcp_service='pc-mcp-http.service',
                      mcp_port=8767, provider_port=14120, lost_runtime=False):
    """Explicit Linux owner action; resets login only, keeps memory and stdio intact.

    If activation fails, HTTP stays stopped. Never automatically roll back to
    credentials from an older generation.
    """
    import sys
    if sys.platform != 'linux': raise ValueError('Activate on the Linux memory server')
    manager = IdentityBackups(backup_config)
    target = Path(target).resolve()
    marker = read(target/'recovery.json')
    candidate, env, receipt, new = runtime_info(target)
    for unit in (identity_service, mcp_service):
        if not re.fullmatch(r'pc-[a-zA-Z0-9_-]+\.service', unit):
            raise ValueError('Invalid Portable Context service name')
    if identity_service == mcp_service or any(type(p) is not int or not 1024 <= p < 65535 for p in (mcp_port, provider_port)):
        raise ValueError('Invalid service ports or names')
    runtime = manager.runtime
    if lost_runtime:
        # Disaster recovery: the previous installation must actually be absent.
        # Never reinstall archived credentials just to satisfy an activation precondition.
        if (runtime/'identity').exists() or (runtime/'oauth.json').exists():
            raise ValueError('Lost-runtime recovery requires absent identity and OAuth configuration')
        old = OAuthConfig(manager.config['issuer'], manager.config['issuer']+'/mcp',
                          f'http://127.0.0.1:{provider_port}', expected_owner)
    else:
        runtime, _, _, old = runtime_info(runtime)
    if (confirmation != 'RESET ACCESS '+old.issuer or marker.get('state') != 'bootstrapped'
            or marker.get('previous_owner_subject') != expected_owner or old.owner_subject != expected_owner
            or new.owner_subject != marker.get('new_owner_subject') or new.owner_subject == expected_owner
            or new.issuer != old.issuer or target == runtime or target.is_relative_to(runtime) or runtime.is_relative_to(target)):
        raise ValueError('Recovery activation confirmation or identity binding mismatch')
    if checksum(candidate/'identity/environment.json') != marker['environment_sha256']:
        raise ValueError('Prepared environment changed')
    # The source process must be stopped; no passkeys, sessions or old data are restored.
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', int(env['PORT']))) == 0:
            raise ValueError('Stop the candidate identity process before activation')
    # Verify systemd really points at this installation before stopping anything.
    for unit, required in ((identity_service, str(runtime/'identity/run.py')),
                           (mcp_service, str(runtime/'oauth.json'))):
        shown = subprocess.run(['systemctl', '--user', 'show', unit, '--property=ExecStart', '--value'],
                               check=True, capture_output=True, text=True).stdout
        if required not in shown:
            raise ValueError('Service is not bound to this recovery runtime')
    if lost_runtime:
        private_directory(runtime)
    else:
        manager.backup()
    with file_lock(manager.state/'recovery.lock'):
        def service(*args):
            return subprocess.run(['systemctl', '--user', *args], check=True, capture_output=True)
        service('stop', mcp_service)
        service('stop', identity_service)
        generation = uuid.uuid4().hex
        pending = runtime/('identity-pending-'+generation)
        retired = runtime/('identity-retired-'+generation)
        try:
            shutil.copytree(candidate/'identity', pending)
            # Runtime ports stay stable for the existing reverse proxy.
            port = int(old.provider_origin.rsplit(':', 1)[1])
            env['PORT'] = str(port); env['ACTORS_PORT'] = str(port+1)
            write(pending/'environment.json', env)
            if not lost_runtime: (runtime/'identity').rename(retired)
            pending.rename(runtime/'identity')
            write(runtime/'oauth.json', {'issuer': new.issuer, 'resource': new.resource,
                  'provider_origin': old.provider_origin, 'owner_subject': new.owner_subject})
            service('start', identity_service)
            api = Bootstrap(port, env['STATIC_API_KEY'])
            for _ in range(100):
                try:
                    owner = api.request('/api/users/'+receipt['owner_subject'])
                    if owner['id'] != new.owner_subject: raise ValueError('Unexpected active owner')
                    break
                except (OSError, RuntimeError): time.sleep(.1)
            else: raise ValueError('Recovered identity did not become ready')
            service('start', mcp_service)
            # systemctl active does not mean Python has finished opening the memory.
            ready = False
            host = urlsplit(old.issuer).netloc
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(180):
                try:
                    req = urllib.request.Request(f'http://127.0.0.1:{mcp_port}/.well-known/oauth-protected-resource/mcp', headers={'Host': host})
                    with opener.open(req, timeout=2) as response:
                        metadata = json.load(response)
                    if metadata.get('authorization_servers') == [new.issuer]: ready = True; break
                except (OSError, ValueError): pass
                time.sleep(1)
            if not ready: raise ValueError('Recovered MCP did not become ready')
            result = {'activated': True, 'new_owner_subject': new.owner_subject, 'generation': generation,
                      'old_credentials_accepted': False, 'requires_new_enrollment': True, 'at': now()}
            write(manager.state/'activation.json', result)
            return result
        except Exception:
            subprocess.run(['systemctl', '--user', 'stop', mcp_service], capture_output=True)
            write(manager.state/'activation-failed.json', {'generation': generation, 'http_stopped': True, 'at': now()})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('configure')
    for name in ('runtime', 'memory-config', 'config', 'repository'): p.add_argument('--'+name, required=True)
    for name in ('init', 'backup', 'status', 'list', 'check', 'restore', 'prune'):
        p = sub.add_parser(name); p.add_argument('--config', required=True)
        if name == 'restore':
            p.add_argument('--snapshot', required=True); p.add_argument('--target', required=True)
    p = sub.add_parser('prepare-recovery')
    for name in ('quarantine', 'target', 'binary', 'expected-origin'): p.add_argument('--'+name, required=True)
    p.add_argument('--port', type=int, default=14120)
    p = sub.add_parser('bootstrap-recovery'); p.add_argument('--target', required=True)
    p = sub.add_parser('owner-login'); p.add_argument('--runtime', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('schedule'); p.add_argument('--config', required=True)
    p = sub.add_parser('export-kit'); p.add_argument('--config', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('restore-config')
    for name in ('kit', 'config', 'runtime', 'memory-home', 'binary', 'expected-origin'): p.add_argument('--'+name, required=True)
    p = sub.add_parser('restore-memory-config')
    for name in ('kit', 'config', 'memory-home', 'binary', 'expected-origin'): p.add_argument('--'+name, required=True)
    p = sub.add_parser('activate-recovery')
    for name in ('target', 'config', 'expected-owner', 'confirm'): p.add_argument('--'+name, required=True)
    p.add_argument('--identity-service', default='pc-identity.service')
    p.add_argument('--mcp-service', default='pc-mcp-http.service')
    p.add_argument('--mcp-port', type=int, default=8767)
    p.add_argument('--provider-port', type=int, default=14120)
    p.add_argument('--lost-runtime', action='store_true')
    args = parser.parse_args()
    if args.command == 'configure': result = configure(args.runtime, args.memory_config, args.config, args.repository)
    elif args.command == 'prepare-recovery': result = prepare_recovery(args.quarantine, args.target, args.binary, args.expected_origin, args.port)
    elif args.command == 'bootstrap-recovery': result = bootstrap_recovery(args.target)
    elif args.command == 'owner-login': result = owner_login(args.runtime, args.output)
    elif args.command == 'schedule': result = install_schedule(args.config)
    elif args.command == 'export-kit': result = export_kit(args.config, args.output)
    elif args.command == 'restore-config': result = restore_config(args.kit, args.config, args.runtime, args.memory_home, args.binary, args.expected_origin)
    elif args.command == 'restore-memory-config': result = restore_memory_config(args.kit, args.config, args.memory_home, args.binary, args.expected_origin)
    elif args.command == 'activate-recovery': result = activate_recovery(args.target, args.config, args.expected_owner, args.confirm,
        identity_service=args.identity_service, mcp_service=args.mcp_service, mcp_port=args.mcp_port,
        provider_port=args.provider_port, lost_runtime=args.lost_runtime)
    else:
        manager = IdentityBackups(args.config)
        try:
            if args.command == 'restore': result = manager.restore(args.snapshot, args.target)
            elif args.command == 'init': result = manager.initialize()
            elif args.command == 'list': result = {'snapshots': [{'id': s['id'], 'time': s['time']} for s in manager.snapshots()]}
            else: result = getattr(manager, args.command)()
        except Exception:
            write(manager.state/'failure.json', {'at': now(), 'operation': args.command, 'diagnostics': 'withheld'})
            raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    os.umask(0o077)
    try: main()
    except Exception:
        # Provider/OS exceptions can contain credentials, paths or response data.
        print('OAuth recovery operation failed; no credentials printed. Check configuration, backup status and private server diagnostics.', file=__import__('sys').stderr)
        raise SystemExit(1)
