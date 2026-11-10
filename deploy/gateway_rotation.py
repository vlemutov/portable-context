"""Resumable mTLS rotation. Public HTTPS certificates remain provider-managed.

Never remove old trust until the new client/server pair has been observed live.
Journal intent before effects; replay a stage after interruption, not a rotation.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from gateway import hostname, issue_credentials, rotation_revision
from setup import private_directory
from portable_context.core import atomic_write, canonical
from portable_context.restic_backup import file_lock

FILES = ('origin-ca.pem','origin.pem','origin-key.pem','edge-ca.pem','edge.pem','edge-key.pem')
STAGES = ('origin_overlap','edge_switch','edge_drain','origin_switch','drain','edge_finalize','origin_finalize','verify')


def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))


def sync_directory(path):
    if os.name!='nt':
        fd=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def durable_text(path, text):
    atomic_write(Path(path),text);Path(path).chmod(0o600);sync_directory(Path(path).parent)


def write(path, value):
    durable_text(path,canonical(value))


def seal_directory(path):
    for item in Path(path).iterdir():
        if item.is_file():
            # Windows FlushFileBuffers requires a writable handle.
            with item.open('r+b') as stream:os.fsync(stream.fileno())
    sync_directory(path)


def utc(): return datetime.now(timezone.utc)


def credentials_info(directory, host):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID
    directory = Path(directory)
    expiry = []; hashes = {}
    for label, usage, dns in [('origin',ExtendedKeyUsageOID.SERVER_AUTH,host),
                              ('edge',ExtendedKeyUsageOID.CLIENT_AUTH,'edge.portable-context.internal')]:
        paths = [directory/(label+suffix) for suffix in ('-ca.pem','.pem','-key.pem')]
        if any(p.is_symlink() or not p.is_file() for p in paths): raise ValueError('Incomplete or linked generation')
        ca, leaf = [x509.load_pem_x509_certificate(p.read_bytes()) for p in paths[:2]]
        key = serialization.load_pem_private_key(paths[2].read_bytes(), password=None)
        encode = lambda k:k.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        if encode(key.public_key()) != encode(leaf.public_key()): raise ValueError('Certificate/key mismatch')
        if leaf.issuer != ca.subject or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError('Invalid generation issuer')
        ca.public_key().verify(leaf.signature,leaf.tbs_certificate_bytes,ec.ECDSA(leaf.signature_hash_algorithm))
        if usage not in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise ValueError('Wrong certificate purpose')
        if dns not in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName):
            raise ValueError('Wrong certificate host')
        expiry.extend([ca.not_valid_after_utc,leaf.not_valid_after_utc])
        for p in paths: hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    expires = min(expiry)
    return {'expires_at':expires.isoformat(),'renew_before':(expires-timedelta(days=30)).isoformat(),'files':hashes}


def initialize(state, config, credentials):
    state = private_directory(state)
    if (state/'config.json').exists(): raise ValueError('Rotation is already configured; use the same state')
    host = hostname(config['public_host']); info = credentials_info(credentials,host)
    if datetime.fromisoformat(info['expires_at']) <= utc()+timedelta(days=2):
        raise ValueError('Initial certificates need at least two days for safe enrollment')
    generation = private_directory(state/'generations/initial')
    for name in FILES:
        shutil.copyfile(Path(credentials)/name,generation/name); (generation/name).chmod(0o600)
    write(generation/'manifest.json',info)
    seal_directory(generation);sync_directory(generation.parent)
    if config.get('origin_config') and Path(config['origin_config']).is_file():
        baseline=state/'pre-rotation-origin.Caddyfile'
        if not baseline.exists():
            baseline.write_bytes(Path(config['origin_config']).read_bytes());baseline.chmod(0o600)
    write(state/'active.json',{'generation':'initial',**{k:info[k] for k in ('expires_at','renew_before')}})
    write(state/'config.json',{**config,'public_host':host,'format':'portable-context-rotation-1'})
    return {'configured':True,**status(state)}


def status(state):
    state = Path(state); active = read(state/'active.json')
    journal = read(state/'rotation.json') if (state/'rotation.json').exists() else None
    remaining = (datetime.fromisoformat(active['expires_at'])-utc()).total_seconds()/86400
    failed = read(state/'failure.json') if (state/'failure.json').exists() else None
    return {'generation':active['generation'],'expires_at':active['expires_at'],'renew_before':active['renew_before'],
        'days_remaining':round(remaining,1),'attention':remaining<14 or failed is not None,
        'stage':journal['stage'] if journal and not journal.get('complete') else 'idle',
        'last_failure':failed,'scheduled':(state/'schedule.json').exists()}


class Rotation:
    def __init__(self,state,backend,clock=utc):
        self.state=Path(state).resolve(); self.backend=backend; self.clock=clock
        self.config=read(self.state/'config.json')
        if self.config.get('format')!='portable-context-rotation-1': raise ValueError('Unknown rotation format')

    def generation(self,name):
        rotation_revision(name)
        path=self.state/'generations'/name
        if path.is_symlink(): raise ValueError('Linked generation refused')
        info=credentials_info(path,self.config['public_host'])
        if info!=read(path/'manifest.json'): raise ValueError('Generation changed after creation')
        return path

    def prepare(self,journal):
        target=self.state/'generations'/journal['new']
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix='.issuing-',dir=target.parent) as tmp:
                source=Path(tmp)/'keys';issue_credentials(source,self.config['public_host'])
                write(source/'manifest.json',credentials_info(source,self.config['public_host']))
                seal_directory(source);source.rename(target);sync_directory(target.parent)
        return self.generation(journal['new'])

    def tick(self,force=False,interrupt=None):
        with file_lock(self.state/'rotation.lock'):
            try:
                return self._tick(force,interrupt)
            except Exception:
                write(self.state/'failure.json',{'at':self.clock().isoformat(),'operation':'rotation',
                    'next':'Retry the same state; inspect certificate status and private connectivity. No trust rollback performed.'})
                raise

    def _tick(self,force,interrupt):
        active=read(self.state/'active.json'); now=self.clock()
        journal=read(self.state/'rotation.json') if (self.state/'rotation.json').exists() else None
        if not journal or journal.get('complete'):
            if not force and now<datetime.fromisoformat(active['renew_before']): return {'status':'not_due',**status(self.state)}
            journal={'id':uuid.uuid4().hex,'old':active['generation'],'new':'g-'+uuid.uuid4().hex,
                'stage':'origin_overlap','created_at':now.isoformat(),'complete':False}
            write(self.state/'rotation.json',journal)
        old=self.generation(journal['old']); new=self.prepare(journal)
        if datetime.fromisoformat(read(new/'manifest.json')['expires_at'])<=now+timedelta(days=1):
            raise ValueError('Prepared generation is too old; explicit recovery required')
        stage=journal['stage']; revision=journal['new']+'-'+stage
        if interrupt: interrupt(stage,'before')
        if stage=='origin_overlap': ready=self.backend.origin(old,[old,new],revision,new)
        elif stage=='edge_switch': ready=self.backend.edge(new,[old,new],revision,journal)
        elif stage=='edge_drain':
            ready=now>=datetime.fromisoformat(journal['drain_until'])
            if ready:ready=self.backend.edge(new,[old,new],journal['new']+'-edge_switch',journal)
        elif stage=='origin_switch': ready=self.backend.origin(new,[old,new],revision,new)
        elif stage=='drain':
            ready=now>=datetime.fromisoformat(journal['drain_until'])
        elif stage=='edge_finalize': ready=self.backend.edge(new,[new],revision,journal)
        elif stage=='origin_finalize': ready=self.backend.origin(new,[new],revision,new)
        elif stage=='verify':
            ready=self.backend.verify(new,old,journal)
        else: raise ValueError('Invalid rotation stage')
        if not ready: return {'status':'waiting','stage':stage,'generation':journal['new']}
        if interrupt: interrupt(stage,'after')
        if stage=='verify':
            info=read(new/'manifest.json')
            write(self.state/'active.json',{'generation':journal['new'],**{k:info[k] for k in ('expires_at','renew_before')}})
            journal.update(complete=True,completed_at=now.isoformat())
        else:
            journal['stage']=STAGES[STAGES.index(stage)+1]
            if stage in ('edge_switch','origin_switch'):
                # Exceeds the edge request timeout. Existing connections can finish.
                seconds=max(660,int(self.config.get('drain_seconds',900)))
                journal['drain_until']=(now+timedelta(seconds=seconds)).isoformat()
        write(self.state/'rotation.json',journal)
        (self.state/'failure.json').unlink(missing_ok=True)
        return {'status':'complete' if journal['complete'] else 'advanced','stage':journal['stage'],'generation':journal['new']}


def materialize(state, label, certificate, trusts, role):
    """Immutable complete file sets: a crash cannot leave a mixed key/cert pair."""
    rotation_revision(label)
    destination=Path(state)/'bundles'/label
    leaf=role; peer='edge' if role=='origin' else 'origin'
    files={leaf+'.pem':(certificate/(leaf+'.pem')).read_bytes(),
           leaf+'-key.pem':(certificate/(leaf+'-key.pem')).read_bytes(),
           peer+'-ca.pem':b''.join((p/(peer+'-ca.pem')).read_bytes() for p in trusts)}
    if destination.exists():
        if destination.is_symlink() or any((destination/n).is_symlink() or (destination/n).read_bytes()!=b for n,b in files.items()):
            raise ValueError('Rotation bundle changed')
        return destination
    private_directory(destination.parent)
    with tempfile.TemporaryDirectory(prefix='.bundle-',dir=destination.parent) as tmp:
        root=Path(tmp)/'files'; root.mkdir(mode=0o700)
        for name,body in files.items(): (root/name).write_bytes(body); (root/name).chmod(0o600)
        seal_directory(root);root.rename(destination);sync_directory(destination.parent)
    return destination


def schedule(state, python, script):
    import sys, subprocess
    if sys.platform!='linux': raise ValueError('Install the timer on the Linux VM, not the client computer')
    state=Path(state).resolve();config=read(state/'config.json')
    if config.get('backend')!='cloudru': raise ValueError('Automatic timer requires the Cloud.ru backend')
    if not (Path(config['cloud_state'])/'credentials.json').is_file(): raise ValueError('Private cloud credentials required on controller host')
    unit='pc-gateway-rotation-'+hashlib.sha256(str(state).encode()).hexdigest()[:12]
    def quoted(value):
        value=str(Path(value).resolve())
        if any(c in value for c in ('%','\n','\r','"','\\')): raise ValueError('Unsupported service path')
        return '"'+value+'"'
    folder=Path.home()/'.config/systemd/user';folder.mkdir(parents=True,exist_ok=True)
    command=' '.join(quoted(p) for p in (python,script))+' --state '+quoted(state)+' run'
    (folder/(unit+'.service')).write_text('[Unit]\nDescription=Portable Context certificate renewal\n[Service]\nType=oneshot\nExecStart='+command+'\nUMask=0077\nNoNewPrivileges=yes\nTimeoutStartSec=240\n',encoding='utf-8')
    (folder/(unit+'.timer')).write_text('[Unit]\nDescription=Check Portable Context transport certificates\n[Timer]\nOnCalendar=*-*-* *:00/10:00\nPersistent=true\nRandomizedDelaySec=45\n[Install]\nWantedBy=timers.target\n',encoding='utf-8')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True,capture_output=True)
    subprocess.run(['systemctl','--user','enable','--now',unit+'.timer'],check=True,capture_output=True)
    write(state/'schedule.json',{'unit':unit,'every_minutes':10,'renew_days_before_expiry':30})
    return {'scheduled':True,'unit':unit+'.timer','every_minutes':10}


def main():
    import sys
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,required=True)
    sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('init');c.add_argument('--config',required=True);c.add_argument('--credentials',required=True)
    c=sub.add_parser('run');c.add_argument('--force',action='store_true')
    sub.add_parser('status');sub.add_parser('schedule')
    a=p.parse_args()
    if a.command=='init':result=initialize(a.state,read(a.config),a.credentials)
    elif a.command=='status':result=status(a.state)
    elif a.command=='schedule':result=schedule(a.state,sys.executable,__file__)
    else:
        from gateway_rotation_cloud import CloudBackend
        active=read(a.state/'active.json')
        pending=read(a.state/'rotation.json') if (a.state/'rotation.json').exists() else None
        if not a.force and utc()<datetime.fromisoformat(active['renew_before']) and (not pending or pending.get('complete')):
            print(json.dumps({'status':'not_due',**status(a.state)}));return
        try:backend=CloudBackend(a.state)
        except Exception:
            write(a.state/'failure.json',{'at':utc().isoformat(),'operation':'cloud_authentication',
                'next':'Check the private controller cloud credentials and network; old trust is unchanged.'})
            raise
        controller=Rotation(a.state,backend)
        for _ in range(len(STAGES)+1):
            result=controller.tick(force=a.force);a.force=False
            if result['status']!='advanced':break
    print(json.dumps(result))


if __name__=='__main__':
    os.umask(0o077)
    try:main()
    except Exception:
        print('Certificate rotation failed; private diagnostics withheld. Retry the same state and check status.',file=__import__('sys').stderr)
        raise SystemExit(1)
