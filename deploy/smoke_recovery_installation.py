"""Disposable Linux/systemd installation: HTTPS write, loss, restore, fresh OAuth.

Uses only temporary directories, unique user services and loopback ports. No
production configuration is read. Requires an available systemd user manager.
"""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import oauth_recovery as recovery
from recovery_setup import protect, receive_kit
from portable_context.core import Store
from portable_context.restic_backup import Manager


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class Browser:
    def __init__(self, context):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def request(self, path, data=None, *, form=False, headers=None):
        path = path.removeprefix('https://memory.example')
        payload = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()) if data is not None else None
        req = urllib.request.Request('https://localhost:14210'+path, data=payload,
            headers={'Host': 'memory.example', 'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json', **(headers or {})})
        try:
            with self.opener.open(req, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code in (302, 303): return error.code, {'redirect': error.headers['Location']}
            return error.code, {}

    def login(self, api, owner):
        code = api.create_owner_login_code(owner)
        status, value = self.request('/api/one-time-access-token/'+code, {})
        assert status == 200 and value['id'] == owner, 'Fresh owner login failed'

    def authorize(self):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {'client_id': 'portable-context-desktop', 'response_type': 'code',
            'redirect_uri': 'http://127.0.0.1:14881/callback', 'scope': 'openid offline_access memory:read memory:write',
            'resource': 'https://memory.example/mcp', 'state': secrets.token_urlsafe(24),
            'code_challenge': challenge, 'code_challenge_method': 'S256'}
        status, result = self.request('/authorize?'+urllib.parse.urlencode(params))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.get('redirect', '')).query)
        consent = False
        if 'interaction' in query:
            interaction_id = query['interaction'][0]
            for _ in range(6):
                status, interaction = self.request('/api/oidc/interactions/'+interaction_id)
                assert status == 200
                step = interaction.get('currentStep')
                assert step in ('authenticate', 'select_account', 'consent')
                consent |= step == 'consent'
                status, completed = self.request('/api/oidc/interactions/'+interaction_id+'/complete',
                    {'step': step, 'scopes': interaction['scopes']})
                assert status == 200
                if completed.get('redirectUrl'):
                    status, result = self.request(completed['redirectUrl'])
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.get('redirect', '')).query)
                    break
        assert query.get('state') == [params['state']] and 'code' in query
        status, tokens = self.request('/api/oidc/token', {'grant_type': 'authorization_code',
            'client_id': params['client_id'], 'code': query['code'][0], 'code_verifier': verifier,
            'redirect_uri': params['redirect_uri'], 'resource': params['resource']}, form=True)
        assert status == 200 and tokens.get('refresh_token') and consent
        return tokens

    def mcp(self, token, tool, arguments, allowed=True):
        status, body = self.request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}, headers={'Authorization': 'Bearer '+token,
            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'})
        if not allowed:
            assert status == 401, 'Old credential accepted by running MCP'
            return
        assert status == 200 and 'error' not in body and not body['result'].get('isError'), 'MCP operation failed'
        result = body['result']
        value = result.get('structuredContent')
        if value is None: value = json.loads(next(c['text'] for c in result['content'] if c['type'] == 'text'))
        return value.get('result', value) if isinstance(value, dict) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('provider', 'caddy', 'restic'): parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    if sys.platform != 'linux': raise ValueError('Run the service acceptance on Linux')
    subprocess.run(['systemctl', '--user', 'show-environment'], check=True, capture_output=True)
    for port in (14210, 14212, 14213, 14214, 14230, 14231):
        with socket.socket() as sock: sock.bind(('127.0.0.1', port))
    unit_names = ['pc-recovery-test-'+uuid.uuid4().hex+'-'+role+'.service' for role in ('identity', 'mcp')]
    units = Path.home()/'.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    proxy = None
    with tempfile.TemporaryDirectory(prefix='pc-full-recovery-') as tmp:
        fixture = Path(tmp); fixture.chmod(0o700)
        root = fixture/'installation'; root.mkdir(mode=0o700)
        runtime = root/'oauth'; auth = runtime/'identity'; auth.mkdir(parents=True, mode=0o700)
        memory = root/'memory'; store = Store(memory)
        origin = 'https://memory.example'
        shutil.copyfile(args.provider, auth/'pocket-id'); (auth/'pocket-id').chmod(0o700)
        assert recovery.checksum(auth/'pocket-id') == recovery.LINUX_AMD64_SHA256
        env = {'APP_URL': origin, 'HOST': '127.0.0.1', 'PORT': '14212', 'ACTORS_HOST': '127.0.0.1', 'ACTORS_PORT': '14213',
            'UI_CONFIG_DISABLED': 'true', 'ALLOW_USER_SIGNUPS': 'disabled', 'REQUIRE_USER_EMAIL': 'false',
            'ENCRYPTION_KEY': secrets.token_urlsafe(48), 'STATIC_API_KEY': secrets.token_urlsafe(48),
            'ANALYTICS_DISABLED': 'true', 'VERSION_CHECK_DISABLED': 'true', 'ICON_LIBRARY_URL': 'disabled',
            'LOG_LEVEL': 'error', 'LOG_QUERY_ARGS': 'false'}
        recovery.write(auth/'environment.json', env)
        (auth/'run.py').write_text("import json,os,pathlib\np=pathlib.Path(__file__).parent\nos.chdir(p)\nos.execve(str(p/'pocket-id'),[str(p/'pocket-id')],json.loads((p/'environment.json').read_text()))\n")
        commands = [[sys.executable, str(auth/'run.py')], [sys.executable, '-m', 'portable_context', '--home', str(memory),
            'mcp', '--transport', 'streamable-http', '--port', '14214', '--oauth-config', str(runtime/'oauth.json')]]
        try:
            for unit, command in zip(unit_names, commands):
                if any('%' in c or '\n' in c or '"' in c for c in command): raise ValueError('Unsupported fixture path')
                package_root = str(Path(__file__).resolve().parents[1]/'src')
                (units/unit).write_text('[Service]\nExecStart='+' '.join('"'+c+'"' for c in command)+'\nEnvironment="PYTHONPATH='+package_root+'"\nUMask=0077\nNoNewPrivileges=yes\nStandardOutput=null\nStandardError=null\n')
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
            subprocess.run(['systemctl', '--user', 'start', unit_names[0]], check=True, capture_output=True)
            api = recovery.Bootstrap(14212, env['STATIC_API_KEY'])
            for _ in range(100):
                try: api.request('/api/users'); break
                except (OSError, RuntimeError): time.sleep(.1)
            receipt = api.configure(origin, auth/'receipt.json'); owner = receipt['owner_subject']
            recovery.write(runtime/'oauth.json', {'issuer': origin, 'resource': origin+'/mcp',
                'provider_origin': 'http://127.0.0.1:14212', 'owner_subject': owner})
            subprocess.run(['systemctl', '--user', 'start', unit_names[1]], check=True, capture_output=True)
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]); now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),False).sign(key,hashes.SHA256()))
            (fixture/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (fixture/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            (fixture/'key.pem').chmod(0o600)
            (fixture/'Caddyfile').write_text('{\n admin off\n auto_https off\n persist_config off\n log default {\n output discard\n }\n}\nhttps://:14210 {\n bind 127.0.0.1\n tls '+str(fixture/'cert.pem')+' '+str(fixture/'key.pem')+'\n @mcp path /mcp /mcp/* /.well-known/oauth-protected-resource/*\n handle @mcp {\n reverse_proxy 127.0.0.1:14214\n }\n handle {\n reverse_proxy 127.0.0.1:14212\n }\n}\n')
            proxy = subprocess.Popen([str(Path(args.caddy).resolve()),'run','--config',str(fixture/'Caddyfile'),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            context = ssl.create_default_context(cafile=str(fixture/'cert.pem')); browser = Browser(context)
            for _ in range(180):
                try:
                    if browser.request('/.well-known/oauth-protected-resource/mcp')[0] == 200: break
                except OSError: pass
                time.sleep(1)
            else: raise ValueError('Test installation did not become ready')
            browser.login(api, owner); tokens = browser.authorize(); access = tokens['access_token']
            records = [{'source':'recovery-test','instance':'synthetic','conversation_id':'chat','message_id':str(i),
                'role':role,'text':text} for i,(role,text) in enumerate([('user','Remember recovery-fixture-amber.'),('assistant','Recorded recovery-fixture-amber.')])]
            assert browser.mcp(access,'append_messages',{'records':records})['inserted'] == 2
            before = browser.mcp(access,'memory_status',{}); workspace = before['workspace_id']
            # Include a genuine original and manual wiki page in the memory backup.
            original = b'Synthetic original for disaster recovery\n'; digest = hashlib.sha256(original).hexdigest()
            original_path = fixture/'source-document.txt'; original_path.write_bytes(original)
            assert store.archive(original_path) == digest
            note = memory/'vault/manual/recovery.md'; note.parent.mkdir(parents=True,exist_ok=True); note.write_text('Recovery fixture wiki evidence\n')
            password = fixture/'password'; password.write_text(secrets.token_urlsafe(48)); password.chmod(0o600)
            memory_config = root/'restic-config.json'
            recovery.write(memory_config, {'home':str(memory),'state':str(root/'backup-state'), 'binary':str(Path(args.restic).resolve()),
                'password_file':str(password),'repository':str(fixture/'memory-repository')})
            backups = Manager(memory, memory_config); backups.initialize(); snapshot = backups.checkpoint()
            identity_config = root/'identity-backup-config.json'
            installed_protection = protect(root,workspace,runtime,schedule=False)
            checkpoint = installed_protection['backup']
            local_receipt = receive_kit(installed_protection,fixture/'client')
            kit = Path(local_receipt['private_directory'])
            assert local_receipt['backup_verified']
            print(json.dumps({'phase':'backed_up','https_messages':2,'separate_identity_backup':True}),flush=True)
            subprocess.run(['systemctl','--user','stop',*unit_names],check=True,capture_output=True)
            # Simulate complete runtime/data loss, retaining only external repositories and kit.
            root.rename(fixture/'lost-installation'); root.mkdir(mode=0o700)
            password.rename(fixture/'lost-password')
            # Rebuild restic configuration from the offline credentials, not old runtime files.
            new_config = root/'identity-backup-config.json'
            recovery.restore_config(kit,new_config,runtime,memory,args.restic,origin)
            recovered_identity = recovery.IdentityBackups(new_config)
            quarantine = fixture/'quarantine'; recovered_identity.restore(checkpoint['snapshot_id'],quarantine)
            new_memory_config = root/'restic-config.json'
            proof = recovery.restore_memory_config(kit,new_memory_config,memory,args.restic,origin)
            assert proof['workspace_id'] == workspace
            backups = Manager(memory,new_memory_config)
            restored = root/'restored-memory'; backups.restore(snapshot['snapshot_id'],restored)
            restored.rename(memory)
            assert (memory/'originals'/digest).read_bytes() == original
            assert (memory/'vault/manual/recovery.md').read_text() == 'Recovery fixture wiki evidence\n'
            candidate = fixture/'candidate'
            recovery.prepare_recovery(quarantine,candidate,args.provider,origin,14230)
            recovery.bootstrap_recovery(candidate)
            result = recovery.activate_recovery(candidate,new_config,owner,'RESET ACCESS '+origin,
                identity_service=unit_names[0],mcp_service=unit_names[1],mcp_port=14214,provider_port=14212,lost_runtime=True)
            assert result['activated']
            browser.mcp(access,'memory_status',{},allowed=False)
            assert browser.request('/api/users/me')[0] == 401
            assert browser.request('/api/oidc/token',{'grant_type':'refresh_token','client_id':'portable-context-desktop',
                'refresh_token':tokens['refresh_token'],'resource':origin+'/mcp'},form=True)[0] in (400,401)
            env = recovery.read(runtime/'identity/environment.json'); owner = result['new_owner_subject']
            api = recovery.Bootstrap(14212,env['STATIC_API_KEY']); fresh = Browser(context)
            assert not api.request('/api/users/'+owner+'/webauthn-credentials')
            fresh.login(api,owner); new_tokens = fresh.authorize()
            status = fresh.mcp(new_tokens['access_token'],'memory_status',{})
            assert status['workspace_id'] == workspace and status['stats']['messages'] == 2
            matches = fresh.mcp(new_tokens['access_token'],'search_messages',{'query':'recovery-fixture-amber'})
            assert len(matches) == 2
            record = {**records[0], 'message_id':'after','text':'New write after recovery'}
            assert fresh.mcp(new_tokens['access_token'],'append_messages',{'records':[record]})['inserted'] == 1
            assert recovered_identity.backup()['snapshot_id'] != checkpoint['snapshot_id']
            again = protect(root,workspace,runtime,schedule=False)
            assert receive_kit(again,fixture/'client')['reused']
            print(json.dumps({'full_service_recovery':True,'same_workspace':True,'messages_restored':2,
                'original_verified':True,'wiki_verified':True,'fresh_consent':True,'new_write':True,
                'old_access_refresh_cookie_denied':True,'identity_backup_after_recovery':True,
                'installer_kit_round_trip':True,
                'physical_passkey_enrollment_tested':False}),flush=True)
        finally:
            subprocess.run(['systemctl','--user','stop',*unit_names],capture_output=True)
            for unit in unit_names: (units/unit).unlink(missing_ok=True)
            subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)
            subprocess.run(['systemctl','--user','reset-failed',*unit_names],capture_output=True)
            if proxy is not None: proxy.terminate(); proxy.wait(timeout=15)
    print(json.dumps({'test_services_and_files_removed':True}),flush=True)


if __name__ == '__main__': main()
