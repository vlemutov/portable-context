"""Isolated real-provider experiment; prints assertions, never OAuth secrets."""
import base64
import sys
import asyncio
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import subprocess
import time
import tempfile
import argparse
import socket
import shutil
import sqlite3
from contextlib import closing
import uuid
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


parser=argparse.ArgumentParser(description='Synthetic Pocket ID/restic recovery acceptance; no production account needed')
for name in ('provider','caddy','restic'):parser.add_argument('--'+name,required=True)
args=parser.parse_args()
provider_binary=Path(args.provider).resolve();caddy_binary=Path(args.caddy).resolve();restic_binary=Path(args.restic).resolve()
for port in (14110,14112,14130):
    with socket.socket() as check:check.bind(('127.0.0.1',port))
fixture_ctx=tempfile.TemporaryDirectory(prefix='pc-oauth-recovery-test-')
fixture=Path(fixture_ctx.name)
root=fixture/'old/identity';root.mkdir(parents=True,mode=0o700)
shutil.copyfile(provider_binary,root/'pocket-id');(root/'pocket-id').chmod(0o700)
environment={'HOST':'127.0.0.1','UI_CONFIG_DISABLED':'true','ALLOW_USER_SIGNUPS':'disabled','REQUIRE_USER_EMAIL':'false','ACTORS_HOST':'127.0.0.1','ACTORS_PORT':'14113','ENCRYPTION_KEY':secrets.token_urlsafe(48),'STATIC_API_KEY':secrets.token_urlsafe(48),'ANALYTICS_DISABLED':'true','VERSION_CHECK_DISABLED':'true','ICON_LIBRARY_URL':'disabled','LOG_LEVEL':'error','LOG_QUERY_ARGS':'false'}
environment.update(APP_URL='https://memory.example', PORT='14112')
(root/'environment.json').write_text(json.dumps(environment));(root/'environment.json').chmod(0o600)
base = environment['APP_URL']
key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
    .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
    .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),False)
    .sign(key,hashes.SHA256()))
(root/'lab.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(root/'lab-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
(root/'lab-key.pem').chmod(0o600)
(root/'lab.Caddyfile').write_text('{\n admin off\n auto_https off\n persist_config off\n log default {\n output discard\n }\n}\nhttps://:14110 {\n bind 127.0.0.1\n tls '+str(root/'lab.pem')+' '+str(root/'lab-key.pem')+'\n reverse_proxy 127.0.0.1:14112\n}\n')
context=ssl.create_default_context(cafile=str(root/'lab.pem'))
log = (root / 'private.log').open('ab')
process = subprocess.Popen([str(root / 'pocket-id')], cwd=root,
    env={**{k:os.environ[k] for k in ('PATH','HOME','USER','LANG','TMP','TEMP') if k in os.environ}, **environment}, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
proxy = subprocess.Popen([str(caddy_binary), 'run', '--config', str(root/'lab.Caddyfile'), '--adapter', 'caddyfile'], stdout=log, stderr=log)
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())


def request(path, data=None, *, method=None, admin=False, form=False, headers=None):
    url = path if path.startswith(base) else base + path
    url = 'https://localhost:14110' + url[len(base):]
    payload = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()) if data is not None else None
    hdr = {'Host': 'memory.example', 'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json', **(headers or {})}
    if admin:
        hdr['X-API-Key'] = environment['STATIC_API_KEY']
    req = urllib.request.Request(url, data=payload, headers=hdr, method=method)
    try:
        with opener.open(req, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        if error.code in (302, 303):
            return error.code, {'redirect': error.headers['Location']}
        raw = error.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return error.code, body


try:
    for _ in range(100):
        try:
            status, discovery = request('/.well-known/openid-configuration')
            if status == 200: break
            time.sleep(.1)
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('Provider did not start')
    assert status == 200
    users = request('/api/users', admin=True)[1]['data']
    owner = next((u for u in users if u['username'] == 'pc-lab-owner'), None)
    if owner is None:
        status, owner = request('/api/users', {'username': 'pc-lab-owner', 'isAdmin': True,
            'firstName': 'Synthetic owner', 'email': 'owner@example.invalid', 'emailVerified': True}, admin=True)
        assert status == 201, ('create test user', status, owner)
    status, clients = request('/api/oidc/clients', admin=True)
    client = next((c for c in clients['data'] if c['id'] == 'pc-lab-client'), None)
    if client is None:
        status, client = request('/api/oidc/clients', {'id': 'pc-lab-client', 'name': 'Portable Context test',
            'callbackURLs': ['http://localhost:14111/callback'], 'isPublic': True, 'pkceEnabled': True,
            'skipConsent': True}, admin=True)
        assert status == 201, ('create public test client', status, client.get('code'))
    resource = base + '/mcp'
    status, apis = request('/api/apis', admin=True)
    api_rows = apis if isinstance(apis, list) else apis.get('data', [])
    api = next((a for a in api_rows if a['resource'] == resource), None)
    if api is None:
        status, api = request('/api/apis', {'name': 'Portable Context lab', 'resource': resource}, admin=True)
        assert status == 201, ('create API', status, api.get('code'))
    status, permissions = request('/api/apis/' + api['id'] + '/permissions',
        {'permissions': [{'key': 'memory:read', 'name': 'Read memory'}]}, method='PUT', admin=True)
    assert status == 200, ('permissions', status)
    status, api = request('/api/apis/' + api['id'], admin=True)
    permission_ids = [p['id'] for p in api['permissions']]
    status, _ = request('/api/apis/' + api['id'] + '/clients/pc-lab-client',
        {'userDelegatedAccess': True, 'clientAccess': False, 'userDelegatedPermissionIds': permission_ids,
         'clientPermissionIds': []}, method='PUT', admin=True)
    assert status in (200, 204), ('grant test client', status)
    status, token = request('/api/users/' + owner['id'] + '/one-time-access-token', {}, admin=True)
    assert status == 201
    status, _ = request('/api/one-time-access-token/' + token['token'], {})
    assert status == 200, ('one-time session', status)

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = {'client_id': 'pc-lab-client', 'response_type': 'code',
        'redirect_uri': 'http://localhost:14111/callback', 'scope': 'openid offline_access memory:read',
        'state': secrets.token_urlsafe(24), 'resource': resource,
        'code_challenge': challenge, 'code_challenge_method': 'S256'}
    status, result = request('/authorize?' + urllib.parse.urlencode(params))
    redirect = result.get('redirect', '')
    parsed = urllib.parse.urlsplit(redirect)
    query = urllib.parse.parse_qs(parsed.query)
    if 'interaction' in query:
        interaction_id = query['interaction'][0]
        for _ in range(5):
            status, interaction = request('/api/oidc/interactions/' + interaction_id)
            assert status == 200, ('interaction', status)
            step = interaction.get('currentStep')
            print(json.dumps({'synthetic_interaction_step': step}))
            assert step in ('authenticate', 'select_account', 'consent'), 'Unexpected interaction'
            status, completed = request('/api/oidc/interactions/' + interaction_id + '/complete',
                {'step': step, 'scopes': interaction['scopes']})
            assert status == 200, ('complete interaction', status, completed.get('code'))
            if completed.get('redirectUrl'):
                status, result = request(completed['redirectUrl'])
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.get('redirect', '')).query)
                break
    assert 'code' in query, ('authorization failed', status, list(query))
    assert query['state'][0] == params['state']
    status, tokens = request('/api/oidc/token', {'grant_type': 'authorization_code',
        'client_id': 'pc-lab-client', 'code': query['code'][0], 'code_verifier': verifier,
        'redirect_uri': params['redirect_uri'], 'resource': resource}, form=True)
    assert status == 200, ('token exchange', status, tokens.get('error'))
    print(json.dumps({'pkce_exchange': True, 'refresh_token': bool(tokens.get('refresh_token')),
                      'token_type': tokens.get('token_type')}))
    access = tokens['access_token']
    header=json.loads(base64.urlsafe_b64decode(access.split('.')[0]+'==='))
    claims=json.loads(base64.urlsafe_b64decode(access.split('.')[1]+'==='))
    print(json.dumps({'algorithm':header.get('alg'),'claims_keys':list(claims),'scope':claims.get('scope'),'scp':claims.get('scp'),'jwks_uri':discovery.get('jwks_uri')}))
    status, introspection = request('/api/oidc/introspect', {'token': access}, form=True,
                                   headers={'Authorization': 'Bearer ' + access})
    print(json.dumps({'introspection_status': status, 'active': introspection.get('active'),
                      'error': introspection.get('error')}))
    sys.path.insert(0,str(root/'source'))
    from portable_context.oauth import OAuthConfig,PocketIDVerifier,http_app
    from portable_context.core import Store
    from portable_context.mcp_server import build_server
    import httpx2
    async def check_mcp(access):
        config=OAuthConfig(base,resource,'http://127.0.0.1:14112',owner['id'])
        verifier=PocketIDVerifier(config)
        assert await verifier.verify_token(access), 'real provider token was rejected'
        server=build_server(Store(root/'synthetic-memory'),oauth_config=config,token_verifier=verifier)
        app=http_app(server,config)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=base,
                headers={'Authorization':'Bearer '+access,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}) as client:
                for tool,arguments,allowed in [('memory_status',{},True),('create_project',{'identifier':'forbidden','title':'Forbidden'},False)]:
                    r=await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':arguments}})
                    assert r.status_code==200
                    if allowed: assert 'workspace_id' in r.text
                    else: assert r.json()['error']['code']==-32003
        print(json.dumps({'real_provider_mcp_read':True,'real_provider_mcp_write_denied':True}))
    asyncio.run(check_mcp(access))
    status, identity = request('/api/oidc/userinfo', headers={'Authorization': 'Bearer ' + access})
    assert status == 200 and identity.get('sub') == owner['id'], ('userinfo before revoke', status)
    print(json.dumps({'userinfo_before_revoke': status, 'subject_matches_owner': True}))
    status, refreshed = request('/api/oidc/token', {'grant_type': 'refresh_token',
        'client_id': 'pc-lab-client', 'refresh_token': tokens['refresh_token'], 'resource': resource}, form=True)
    assert status == 200, ('refresh', status, refreshed.get('error'))
    access = refreshed['access_token']
    status, identity = request('/api/oidc/userinfo', headers={'Authorization': 'Bearer ' + access})
    assert status == 200, ('refreshed userinfo', status)
    # Snapshot contains a still-authorized session. Revoke it only AFTER the snapshot.
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import oauth_recovery as recovery
    oldconfig={'issuer':base,'resource':resource,'provider_origin':'http://127.0.0.1:14112','owner_subject':owner['id']}
    recovery.write(root.parent/'oauth.json',oldconfig)
    recovery.write(root/'receipt.json',{'origin':base,'owner_subject':owner['id']})
    password=fixture/'password';password.write_text(secrets.token_urlsafe(32));password.chmod(0o600)
    memory_config=fixture/'memory-config.json'
    recovery.write(memory_config,{'binary':str(restic_binary),'repository':str(fixture/'unused-memory-repository'),'password_file':str(password),'home':str(root/'synthetic-memory'),'state':str(fixture/'memory-state')})
    backup_config=fixture/'backups/identity-backup-config.json'
    recovery.configure(root.parent,memory_config,backup_config,str(fixture/'identity-repository'))
    # Seed a historical passkey record (not a physical authenticator) to check that
    # recovery does not bring back credentials deleted after the snapshot.
    old_passkey=str(uuid.uuid4())
    with closing(sqlite3.connect(root/'data/pocket-id.db')) as db:
        created_at=db.execute('SELECT created_at FROM users WHERE id=?',(owner['id'],)).fetchone()[0]
        db.execute('INSERT INTO webauthn_credentials(id,name,credential_id,public_key,user_id,transport,created_at,attestation_type) VALUES (?,?,?,?,?,?,?,?)',
                   (old_passkey,'synthetic historical passkey',b'fixture-id',b'fixture-public-key',owner['id'],b'[]',created_at,'none'))
        db.commit()
    backups=recovery.IdentityBackups(backup_config);backups.initialize();checkpoint=backups.backup()
    with closing(sqlite3.connect(root/'data/pocket-id.db')) as db:
        db.execute('DELETE FROM webauthn_credentials WHERE id=?',(old_passkey,));db.commit()
    old_jwks=request('/.well-known/jwks.json')[1]
    status, _ = request('/api/oidc/users/me/authorized-clients/pc-lab-client', method='DELETE')
    assert status == 204, ('revoke', status)
    status, identity = request('/api/oidc/userinfo', headers={'Authorization': 'Bearer ' + access})
    assert status == 401, ('userinfo after revoke', status)
    refresh_status, rejected = request('/api/oidc/token', {'grant_type': 'refresh_token',
        'client_id': 'pc-lab-client', 'refresh_token': refreshed['refresh_token'], 'resource': resource}, form=True)
    assert refresh_status == 400 and rejected.get('error') == 'invalid_grant', ('revoked refresh', refresh_status)
    async def rejected_mcp():
        verifier=PocketIDVerifier(OAuthConfig(base,resource,'http://127.0.0.1:14112',owner['id']))
        assert await verifier.verify_token(access) is None
    asyncio.run(rejected_mcp())
    # Restore a pre-revocation archive but start a fresh identity generation.
    quarantine=fixture/'quarantine';backups.restore(checkpoint['snapshot_id'],quarantine)
    fresh=fixture/'fresh';recovery.prepare_recovery(quarantine,fresh,root/'pocket-id',base,14130)
    recovery.bootstrap_recovery(fresh)
    fresh_env=recovery.read(fresh/'identity/environment.json');fresh_receipt=recovery.read(fresh/'identity/receipt.json')
    fresh_process=subprocess.Popen([str(fresh/'identity/pocket-id')],cwd=fresh/'identity',env=recovery.provider_environment(fresh_env),stdout=log,stderr=log)
    fresh_api=recovery.Bootstrap(14130,fresh_env['STATIC_API_KEY'])
    try:
        for _ in range(100):
            try:fresh_api.request('/api/users');break
            except (OSError,RuntimeError):time.sleep(.1)
        else:raise RuntimeError('Fresh provider did not start')
        async def restored_rejects_old():
            verifier=recovery.OAuthConfig.load(fresh/'oauth.json')
            assert await PocketIDVerifier(verifier).verify_token(access) is None
        asyncio.run(restored_rejects_old())
        direct=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with direct.open('http://127.0.0.1:14130/.well-known/jwks.json',timeout=5) as response:new_jwks=json.load(response)
        assert not ({k['kid'] for k in old_jwks['keys']} & {k['kid'] for k in new_jwks['keys']})
        jar=next(h.cookiejar for h in opener.handlers if isinstance(h,urllib.request.HTTPCookieProcessor))
        old_cookie='; '.join(c.name+'='+c.value for c in jar)
        def fresh_status(path,data=None,headers=None):
            req=urllib.request.Request('http://127.0.0.1:14130'+path,data=data,headers=headers or {})
            try:
                with direct.open(req,timeout=5) as response:return response.status,response.read()
            except urllib.error.HTTPError as error:return error.code,b''
        cookie_status,_=fresh_status('/api/users/me',headers={'Cookie':old_cookie})
        assert cookie_status==401,('old cookie',cookie_status)
        refresh_status_new,_=fresh_status('/api/oidc/token',urllib.parse.urlencode({'grant_type':'refresh_token','client_id':'pc-lab-client','refresh_token':refreshed['refresh_token'],'resource':resource}).encode(),{'Content-Type':'application/x-www-form-urlencoded'})
        assert refresh_status_new in (400,401)
        newowner=fresh_receipt['owner_subject'];assert newowner!=owner['id']
        assert not fresh_api.request('/api/users/'+newowner+'/webauthn-credentials')
        code=fresh_api.create_owner_login_code(newowner)
        login_status,login_body=fresh_status('/api/one-time-access-token/'+code,b'{}',{'Content-Type':'application/json'})
        assert login_status==200 and json.loads(login_body)['id']==newowner
        print(json.dumps({'recovery_real_provider':True,'snapshot_preceded_revocation':True,'old_access_denied':True,'old_refresh_denied':True,'old_cookie_denied':True,'signing_keys_changed':True,'old_passkeys_not_restored':True,'new_owner_login_succeeded':True}))
    finally:
        fresh_process.terminate();fresh_process.wait(timeout=15)
    # A code that has already been redeemed must not issue a second token.
    replay_status, replay = request('/api/oidc/token', {'grant_type':'authorization_code',
        'client_id':'pc-lab-client','code':query['code'][0],'code_verifier':verifier,
        'redirect_uri':params['redirect_uri'],'resource':resource},form=True)
    assert replay_status==400 and replay.get('error')=='invalid_grant'
    # New authorization with the same challenge, but the wrong verifier.
    status, again=request('/authorize?'+urllib.parse.urlencode(params))
    wrong_code=urllib.parse.parse_qs(urllib.parse.urlsplit(again['redirect']).query)['code'][0]
    wrong_status, wrong=request('/api/oidc/token',{'grant_type':'authorization_code',
        'client_id':'pc-lab-client','code':wrong_code,'code_verifier':secrets.token_urlsafe(48),
        'redirect_uri':params['redirect_uri'],'resource':resource},form=True)
    assert wrong_status==400
    print(json.dumps({'refresh':True,'userinfo_after_revoke':401,'refresh_after_revoke':refresh_status,
        'mcp_revoked_token_denied':True,'code_replay_denied':True,'wrong_pkce_denied':True}))

finally:
    proxy.terminate()
    proxy.wait(timeout=15)
    process.terminate()
    process.wait(timeout=15)
    log.close()
    fixture_ctx.cleanup()
