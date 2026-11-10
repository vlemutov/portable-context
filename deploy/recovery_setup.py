"""Installer integration for private, offline recovery kits.

The SSH response contains secrets and must only be consumed by receive_kit,
never printed, logged or returned by an agent-facing tool.
"""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import uuid

from setup import private_directory, save

README = '''Portable Context — комплект восстановления

Эта папка содержит пароль резервных копий и, для S3, ключ доступа к хранилищу.
Не отправляйте её в чат, GitHub или общую папку. Сохраните защищённую копию
вне сервера и этого компьютера (например, в зашифрованном внешнем хранилище).

kit.json: адреса репозиториев резервных копий, HTTPS-адрес и ID памяти.
restic-password: пароль шифрования. s3-config.json: доступ к S3, если настроен.
Инструкция: docs/oauth-recovery.md в репозитории Portable Context.
Восстановление OAuth создаёт новый вход: старые ключи и разрешения не возвращаются.
После замены ключей S3 получите новый комплект командой setup.py recovery-kit.
'''


def protect(root, workspace, runtime=None, schedule=True):
    """Runs only on the owner server, under the installed Python environment."""
    import oauth_recovery as recovery
    root = Path(root).resolve()
    db = root/'memory/context.sqlite3'
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as con:
        actual = con.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    if actual != workspace: raise ValueError('Connected to a different memory; no kit exported')
    if runtime is None:
        candidates = [root/name for name in ('oauth', 'oauth10') if (root/name/'oauth.json').is_file()]
        if not candidates: return {'configured':False, 'reason':'oauth_not_installed',
            'next':'After OAuth setup, run setup.py recovery-kit with the same private state directory.'}
        if len(candidates) != 1: raise ValueError('Multiple OAuth runtimes; select one explicitly')
        runtime = candidates[0]
    runtime = Path(runtime).resolve()
    if runtime.parent != root: raise ValueError('OAuth runtime must belong to this server installation')
    memory_config = root/'restic-config.json'; base = recovery.read(memory_config)
    config = root/'identity-backup-config.json'
    first_setup = not config.exists()
    repository = recovery.read(config)['repository'] if config.exists() else base['repository'].rstrip('/')+'-identity'
    recovery.configure(runtime,memory_config,config,repository)
    manager = recovery.IdentityBackups(config)
    # A failed connection must never be interpreted as a missing repository.
    marker = manager.state/'initialization-attempted.json'
    if first_setup:
        # restic init refuses an existing repository. A retry only opens it;
        # failed initialization requires diagnosis, never a fallback after network failure.
        recovery.write(marker, {'attempted':True})
        manager.initialize()
    manager.snapshots()
    backup = manager.backup(); check = manager.check()
    scheduled = recovery.install_schedule(config) if schedule else None
    with tempfile.TemporaryDirectory(prefix='kit-export-',dir=root) as tmp:
        output = Path(tmp)/'kit'; recovery.export_kit(config,output)
        files = {p.name:base64.b64encode(p.read_bytes()).decode() for p in output.iterdir()}
    return {'configured':True, 'workspace_id':workspace, 'backup':backup, 'verified':check['verified'],
            'schedule':scheduled, 'kit_files':files}


def receive_kit(result, state, output=None):
    """Consume a secret response and return a strictly redacted local receipt."""
    if not result.get('configured'):
        return {'configured':False,'reason':'oauth_not_installed','next':result.get('next')}
    if result.get('verified') is not True:
        raise ValueError('Backup restore verification must succeed before kit handoff')
    encoded = result.get('kit_files', {})
    if set(encoded) not in ({'kit.json','restic-password'}, {'kit.json','restic-password','s3-config.json'}):
        raise ValueError('Invalid recovery kit file list')
    if any(not isinstance(v,str) or len(v)>65536 for v in encoded.values()): raise ValueError('Recovery kit too large')
    files = {name:base64.b64decode(value,validate=True) for name,value in encoded.items()}
    manifest = json.loads(files['kit.json'])
    from gateway import hostname
    origin = manifest.get('issuer','')
    if origin != 'https://'+hostname(origin.removeprefix('https://')): raise ValueError('Invalid recovery issuer')
    if manifest.get('format') != 'portable-context-identity-1': raise ValueError('Invalid recovery format')
    if set(manifest.get('files',{})) != set(files)-{'kit.json'}: raise ValueError('Invalid recovery manifest')
    if any(hashlib.sha256(files[name]).hexdigest()!=digest for name,digest in manifest['files'].items()):
        raise ValueError('Recovery kit checksum mismatch')
    if (manifest.get('memory') or {}).get('workspace_id') != result['workspace_id']:
        raise ValueError('Recovery kit belongs to another memory')
    state = private_directory(state)
    destination = Path(output).expanduser().resolve() if output else state/'recovery-kit'
    reused = False
    if destination.exists():
        reused = all((destination/name).is_file() and not (destination/name).is_symlink()
                     and (destination/name).read_bytes()==value for name,value in files.items())
        if not reused:
            if output: raise ValueError('Existing kit differs; select a new output directory')
            destination = state/('recovery-kit-'+uuid.uuid4().hex[:12])
    private_directory(destination.parent)
    if destination.is_symlink(): raise ValueError('Recovery directory must not be a symlink')
    if not reused:
        with tempfile.TemporaryDirectory(prefix='.recovery-kit-',dir=destination.parent) as tmp:
            staging = private_directory(tmp)
            for name, value in files.items():
                (staging/name).write_bytes(value); (staging/name).chmod(0o600)
            (staging/'README.txt').write_text(README,encoding='utf-8')
            # Move the fully verified tree; interrupted transfers never look complete.
            staging.rename(destination)
    private_directory(destination)
    receipt = {'configured':True,'private_directory':str(destination),'reused':reused,
        'workspace_id':result['workspace_id'],'backup_verified':result['verified'],
        'next':'Save an additional protected copy outside this computer; never paste kit contents in chat.'}
    save(state/'recovery-kit-receipt.json',receipt)
    return receipt


# Run via the established encrypted-stdin installer transport. No shell arguments contain keys.
REMOTE = '''import json,sys,pathlib,hashlib,subprocess
p=json.load(sys.stdin);root=pathlib.Path(p['root']).resolve()
raw=json.dumps(p['files'],sort_keys=True).encode();digest=hashlib.sha256(raw).hexdigest()
tools=root/'recovery-tools'/digest;tools.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,content in p['files'].items():
 path=tools/name
 if pathlib.Path(name).name!=name or not name.endswith('.py'):raise ValueError('Invalid helper name')
 path.write_text(content,encoding='utf-8');path.chmod(0o600)
program="import sys,json;sys.path.insert(0,sys.argv[1]);from recovery_setup import protect;p=json.load(sys.stdin);print(json.dumps(protect(p['root'],p['workspace'],p.get('runtime'),p['schedule'])))"
runtime=pathlib.Path(p['runtime']).resolve() if p.get('runtime') else None
if runtime is None:
 candidates=[root/name for name in ('oauth','oauth10') if (root/name/'oauth.json').is_file()]
 if len(candidates)==1:runtime=candidates[0]
if runtime is not None and runtime.parent!=root:raise ValueError('Runtime outside this installation')
python=root/'current/venv/bin/python'
if not python.is_file() and runtime is not None:python=runtime/'venv/bin/python'
r=subprocess.run([str(python),'-c',program,str(tools)],input=json.dumps(p),text=True,capture_output=True)
if r.returncode:raise RuntimeError('Recovery setup failed; secret output withheld')
sys.stdout.write(r.stdout)
'''
