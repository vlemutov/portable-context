"""Real isolated Linux install -> OAuth write -> killed update -> resume.

Uses temporary memory, unique systemd units, real Pocket ID/restic/Caddy. No cloud
account, user memory, production credentials or public ingress changes.
"""
import argparse
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import zipfile
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from setup import bundle, ROOT, read
from gateway import issue_credentials
from https_runtime import configure,describe,workspace,unit_file
from smoke_recovery_installation import Browser
from pocketid import Bootstrap
from recovery_setup import protect,receive_kit
import remote_install


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('provider','caddy','restic'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    if sys.platform!='linux':raise ValueError('Run this acceptance on Linux/systemd')
    for number in (14210,14212,14213,14214,14443):
        with socket.socket() as sock:sock.bind(('127.0.0.1',number))
    subprocess.run(['systemctl','--user','show-environment'],check=True,capture_output=True)
    with tempfile.TemporaryDirectory(prefix='pc-https-installer-') as tmp:
        fixture=Path(tmp);root=fixture/'installation';proxy=None;manifest=None
        payload={**bundle(),'recovery_password':secrets.token_urlsafe(48),'schedule_backup':False,
            'restic_installer':'def install(directory): return '+repr(str(Path(args.restic).resolve())),
            'https_helpers':{name:(ROOT/'deploy'/name).read_text() for name in ('https_runtime.py','gateway.py','pocketid.py','setup.py')}}
        try:
            first=remote_install.install(payload,root);identifier=first['memory']['workspace_id']
            generation=fixture/'generation';issue_credentials(generation,'memory.example')
            ports={'identity_port':14212,'application_port':14214,'origin_port':14443}
            # Death/failure after intent is persisted, before any HTTP unit exists.
            with patch('https_runtime.download_provider',side_effect=RuntimeError('synthetic bootstrap interruption')):
                try:configure(root,identifier,'https://memory.example',generation,ports=ports)
                except RuntimeError:pass
                else:raise AssertionError('Bootstrap did not interrupt')
            partial=remote_install.install(payload,root)
            assert partial['https'] is None and partial['memory']['workspace_id']==identifier
            configured=configure(root,identifier,'https://memory.example',generation,caddy=args.caddy,provider=args.provider,ports=ports)
            manifest=describe(root);owner=configured['owner_subject'];runtime=Path(configured['runtime'])
            assert subprocess.run(['loginctl','show-user',str(os.getuid()),'--property=Linger','--value'],check=True,capture_output=True,text=True).stdout.strip()=='yes'
            # Local HTTPS represents the managed public endpoint; never skip verification.
            key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
            cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),False).sign(key,hashes.SHA256()))
            (fixture/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (fixture/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));(fixture/'key.pem').chmod(0o600)
            # The fixture public TLS proxy connects through the actual mTLS origin.
            config='{\n admin off\n auto_https off\n persist_config off\n log default {\n output discard\n }\n}\nhttps://:14210 {\n bind 127.0.0.1\n tls '+str(fixture/'cert.pem')+' '+str(fixture/'key.pem')+'\n reverse_proxy https://127.0.0.1:14443 {\n header_up Host memory.example\n transport http {\n tls_server_name memory.example\n tls_trust_pool file '+str(generation/'origin-ca.pem')+'\n tls_client_auth '+str(generation/'edge.pem')+' '+str(generation/'edge-key.pem')+'\n }\n }\n}\n'
            (fixture/'Caddyfile').write_text(config)
            proxy=subprocess.Popen([args.caddy,'run','--config',str(fixture/'Caddyfile'),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            browser=Browser(ssl.create_default_context(cafile=str(fixture/'cert.pem')))
            for _ in range(100):
                try:
                    if browser.request('/.well-known/oauth-protected-resource/mcp')[0]==200:break
                except OSError:pass
                time.sleep(.1)
            else:raise ValueError('Fixture HTTPS did not become ready')
            env=read(runtime/'identity/environment.json');api=Bootstrap(14212,env['STATIC_API_KEY'])
            browser.login(api,owner);token=browser.authorize()['access_token']
            record={'source':'https-install-test','instance':'synthetic','conversation_id':'one','message_id':'one','role':'user','text':'Preserve installed memory across interrupted update.'}
            assert browser.mcp(token,'append_messages',{'records':[record]})['inserted']==1
            store=__import__('portable_context.core',fromlist=['Store']).Store(root/'memory')
            original=fixture/'original.txt';original.write_text('Original evidence');blob=store.archive(original)
            wiki=root/'memory/vault/manual/test.md';wiki.parent.mkdir(parents=True,exist_ok=True);wiki.write_text('User-owned wiki')
            old_identity=(runtime/'oauth.json').read_bytes();old_key=(runtime/'identity/environment.json').read_bytes()
            old_transport=(root/'gateway10/keys/origin.pem').read_bytes()
            # Repeat runtime setup preserves owner/grants/keys; first kit is restored and verified.
            configure(root,identifier,'https://memory.example',generation,caddy=args.caddy,provider=args.provider,ports=ports)
            protected=protect(root,identifier,runtime,schedule=False)
            kit=receive_kit(protected,fixture/'client');assert kit['backup_verified']
            # Different immutable package release, same data/identity.
            archive=io.BytesIO(base64.b64decode(payload['bundle']));updated=io.BytesIO()
            with zipfile.ZipFile(archive) as before,zipfile.ZipFile(updated,'w',zipfile.ZIP_DEFLATED) as after:
                for item in before.infolist():
                    data=before.read(item.filename)
                    if item.filename=='README.md':data+=b'\nSynthetic installer update fixture.\n'
                    after.writestr(item.filename,data)
            changed={**payload,'bundle':base64.b64encode(updated.getvalue()).decode(),'sha256':hashlib.sha256(updated.getvalue()).hexdigest()}
            # Real process death immediately after stopping the managed HTTP writer.
            program="import sys,json,os;sys.path.insert(0,sys.argv[1]);import https_runtime,remote_install;original=https_runtime.quiesce\ndef stop(root):\n original(root);os._exit(77)\nhttps_runtime.quiesce=stop\nremote_install.install(json.load(sys.stdin),sys.argv[2])"
            died=subprocess.run([sys.executable,'-c',program,str(ROOT/'deploy'),str(root)],input=json.dumps(changed),text=True,capture_output=True,timeout=240)
            assert died.returncode==77,'Did not reach update interruption point'
            assert (root/'maintenance.json').is_file()
            failed=subprocess.run([str(root/'bin/mcp')],capture_output=True,timeout=10)
            assert failed.returncode!=0,'SSH writer escaped maintenance gate'
            # Kill a second retry after the candidate has opened/migrated the DB.
            # The saved checkpoint must prevent running old-code backup on resume.
            program="import sys,json,os;sys.path.insert(0,sys.argv[1]);import remote_install;original=remote_install.run\ndef stop(args,**kwargs):\n result=original(args,**kwargs)\n if args[-1]=='init' and 'portable_context' in args:os._exit(78)\n return result\nremote_install.run=stop\nremote_install.install(json.load(sys.stdin),sys.argv[2])"
            died=subprocess.run([sys.executable,'-c',program,str(ROOT/'deploy'),str(root)],input=json.dumps(changed),text=True,capture_output=True,timeout=240)
            assert died.returncode==78,'Did not reach post-migration interruption point'
            assert read(root/'maintenance.json')['checkpoint_complete']
            backup_wrapper=root/'bin/backup';backup_wrapper.write_text('#!/bin/sh\nexit 91\n');backup_wrapper.chmod(0o700)
            second=remote_install.install(changed,root)
            assert second['release']!=first['release'] and second['memory']['workspace_id']==identifier
            assert second['https']['ready'] and not (root/'maintenance.json').exists()
            status=browser.mcp(token,'memory_status',{})
            assert status['workspace_id']==identifier and status['stats']['messages']==1
            assert browser.mcp(token,'append_messages',{'records':[record]})['unchanged']==1
            assert (runtime/'oauth.json').read_bytes()==old_identity and (runtime/'identity/environment.json').read_bytes()==old_key
            assert (root/'gateway10/keys/origin.pem').read_bytes()==old_transport
            assert wiki.read_text()=='User-owned wiki'
            assert (root/'memory/originals'/blob).read_bytes()==original.read_bytes()
            # A failed readiness check after activation closes both entry paths;
            # repeating the same release repairs the service without old-code rollback.
            program="import sys,json;sys.path.insert(0,sys.argv[1]);import https_runtime,remote_install\ndef fail(*args,**kwargs):raise RuntimeError('synthetic readiness failure')\nhttps_runtime.metadata=fail\nremote_install.install(json.load(sys.stdin),sys.argv[2])"
            rejected=subprocess.run([sys.executable,'-c',program,str(ROOT/'deploy'),str(root)],input=json.dumps(changed),text=True,capture_output=True,timeout=240)
            assert rejected.returncode!=0 and read(root/'maintenance.json')['operation']=='https-readiness'
            assert subprocess.run([str(root/'bin/mcp')],capture_output=True,timeout=10).returncode!=0
            third=remote_install.install(changed,root);assert third['release']==second['release']
            assert browser.mcp(token,'memory_status',{})['stats']['messages']==1
            print(json.dumps({'clean_install':True,'https_oauth_write':True,'repeated_install':True,
                'partial_bootstrap_resumed':True,'killed_update_resumed':True,'post_migration_resume_skips_old_code':True,
                'identity_and_tokens_preserved':True,'memory_originals_wiki_preserved':True,
                'readiness_failure_closed_and_recovered':True,'owner_services_survive_logout':True,
                'recovery_kit_verified':True,'http_runs_current_release':True}),flush=True)
        finally:
            if proxy:proxy.terminate();proxy.wait(timeout=10)
            if (root/'https-runtime.json').exists():manifest=read(root/'https-runtime.json')
            if manifest:
                for key in ('mcp_service','identity_service','origin_service'):
                    subprocess.run(['systemctl','--user','disable','--now',manifest[key]],capture_output=True)
                    unit_file(manifest[key]).unlink(missing_ok=True)
                subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)


if __name__=='__main__':main()
