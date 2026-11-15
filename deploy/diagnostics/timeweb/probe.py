"""HTTPS-only synthetic checks. Secrets remain in memory; return safe evidence."""
import base64
import hashlib
import http.cookiejar
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Probe:
    def __init__(self, base, key):
        self.base, self.key = base, key
        self.opener = urllib.request.build_opener(NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.results = {}

    def open(self, path, data=None, method=None, headers=None, form=False, timeout=90):
        if path.startswith(self.base + '/'):
            path = path[len(self.base):]
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Refusing a request outside the test HTTPS origin')
        hdr = {'Accept-Encoding': 'identity', **(headers or {})}
        if isinstance(data, dict):
            hdr['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
        try:
            return self.opener.open(urllib.request.Request(self.base + path,
                data=data, method=method, headers=hdr), timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc

    def request(self, path, data=None, **kwargs):
        with self.open(path, data, **kwargs) as response:
            if response.status in (302, 303):
                return response.status, {'redirect': response.headers['Location']}
            raw = response.read(9 * 1024 * 1024)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {'non_json': True}
            return response.status, body

    def check(self, name, condition, evidence=True):
        self.results[name] = evidence if condition else False
        if not condition:
            raise RuntimeError('Check failed: ' + name)

    def code(self):
        verifier = secrets.token_urlsafe(48)
        params = {'client_id': 'pc-lab-client', 'response_type': 'code',
            'redirect_uri': 'http://localhost:14111/callback',
            'scope': 'openid offline_access memory:read memory:write',
            'state': secrets.token_urlsafe(24), 'resource': self.base + '/mcp',
            'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
            'code_challenge_method': 'S256'}
        status, result = self.request('/authorize?' + urllib.parse.urlencode(params))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.get('redirect', '')).query)
        if 'interaction' in query:
            iid = query['interaction'][0]
            for _ in range(5):
                status, interaction = self.request('/api/oidc/interactions/' + iid)
                self.check('interaction_read', status == 200)
                step = interaction.get('currentStep')
                self.check('interaction_known_step', step in ('authenticate', 'select_account', 'consent'))
                status, result = self.request('/api/oidc/interactions/' + iid + '/complete',
                    {'step': step, 'scopes': interaction['scopes']})
                self.check('interaction_complete', status == 200)
                if step == 'consent':
                    self.results['consent'] = True
                if result.get('redirectUrl'):
                    status, result = self.request(result['redirectUrl'])
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result.get('redirect', '')).query)
                    break
        self.check('authorization_state', 'code' in query and query.get('state') == [params['state']])
        return {'grant_type': 'authorization_code', 'client_id': 'pc-lab-client',
            'code': query['code'][0], 'code_verifier': verifier,
            'redirect_uri': params['redirect_uri'], 'resource': self.base + '/mcp'}

    def run(self):
        status, _ = self.request('/_diag/configure', {'origin': self.base})
        self.check('bootstrap_unauthorized_denied', status == 401)
        status, configured = self.request('/_diag/configure', {'origin': self.base},
            headers={'X-Diagnostic-Key': self.key})
        self.results['configure_response'] = {'status': status, **{k: configured[k] for k in ('error', 'stage', 'reason') if k in configured}}
        self.check('configure', status == 200)
        status, discovery = self.request('/.well-known/openid-configuration')
        self.check('https_discovery_issuer', status == 200 and discovery.get('issuer') == self.base)
        self.check('pkce_s256_advertised', 'S256' in discovery.get('code_challenge_methods_supported', []))
        status, _ = self.request('/api/users')
        self.check('provider_admin_not_public', status == 404)
        status, login = self.request('/_diag/login-code', {}, headers={'X-Diagnostic-Key': self.key})
        self.check('synthetic_login_code', status == 200)
        status, _ = self.request('/api/one-time-access-token/' + login['code'], {})
        self.check('synthetic_owner_login', status == 200)
        payload = self.code()
        status, rejected = self.request('/api/oidc/token', {**payload, 'code_verifier': secrets.token_urlsafe(48)}, form=True)
        self.check('wrong_pkce_denied', status == 400)
        payload = self.code()
        status, tokens = self.request('/api/oidc/token', payload, form=True)
        self.check('pkce_exchange', status == 200 and bool(tokens.get('refresh_token')))
        status, _ = self.request('/api/oidc/token', payload, form=True)
        self.check('code_replay_denied', status == 400)
        access = tokens['access_token']
        headers = {'Authorization': 'Bearer ' + access}
        status, identity = self.request('/api/oidc/userinfo', headers=headers)
        self.check('owner_identity', status == 200 and identity.get('sub') == login['owner'])
        for path in ('/files/not-a-file', '/events'):
            status, _ = self.request(path)
            self.check('unauthorized_' + path.split('/')[1], status == 401)
        status, _ = self.request('/events', headers={'Authorization': 'Bearer invalid'})
        self.check('invalid_token_denied', status == 401)
        # Deterministic, non-sensitive bytes; memory bound: one 4 MiB file.
        data = bytes(range(256)) * (4 * 1024 * 1024 // 256)
        digest = hashlib.sha256(data).hexdigest()
        started = time.monotonic()
        status, uploaded = self.request('/files', data, headers=headers)
        self.check('file_upload', status == 201 and uploaded.get('sha256') == digest,
            {'bytes': len(data), 'sha256': digest, 'seconds': round(time.monotonic() - started, 2)})
        status, duplicate = self.request('/files', data, headers=headers)
        self.check('file_duplicate_idempotent', status == 200 and duplicate.get('duplicate') is True and duplicate.get('sha256') == digest)
        with self.open('/files/' + digest, headers=headers) as response:
            actual = response.read(len(data) + 1)
            self.check('file_download_sha256', response.status == 200 and hashlib.sha256(actual).hexdigest() == digest)
        for seconds, last_id in ((6, 0), (3, 6), (65, 0)):
            received, ids = [], []
            started = time.monotonic()
            with self.open('/events?seconds=' + str(seconds), headers={**headers, 'Last-Event-ID': str(last_id)}) as response:
                self.check('sse_content_type', response.status == 200 and response.headers.get('Content-Type', '').startswith('text/event-stream'))
                while True:
                    line = response.readline()
                    if not line:
                        break
                    if line.startswith(b'id: '):
                        received.append(time.monotonic() - started)
                        ids.append(int(line[4:]))
            evidence = {'events': len(ids), 'first_seconds': round(received[0], 2) if received else None,
                'last_seconds': round(received[-1], 2) if received else None}
            self.check('sse_' + str(seconds) + 's',
                ids == list(range(last_id + 1, last_id + seconds + 1)) and received[-1] - received[0] > seconds - 2
                and received[0] < 15, evidence)
        status, refreshed = self.request('/api/oidc/token', {'grant_type': 'refresh_token',
            'client_id': 'pc-lab-client', 'refresh_token': tokens['refresh_token'], 'resource': self.base + '/mcp'}, form=True)
        self.check('refresh', status == 200 and bool(refreshed.get('access_token')))
        headers = {'Authorization': 'Bearer ' + refreshed['access_token']}
        status, _ = self.request('/files/' + digest, method='DELETE', headers=headers)
        self.check('file_delete_with_refreshed_token', status == 200)
        status, _ = self.request('/api/oidc/users/me/authorized-clients/pc-lab-client', method='DELETE')
        self.check('revoke', status == 204)
        status, _ = self.request('/events', headers=headers)
        self.check('revoked_access_denied', status == 401)
        status, _ = self.request('/api/oidc/token', {'grant_type': 'refresh_token',
            'client_id': 'pc-lab-client', 'refresh_token': refreshed['refresh_token'], 'resource': self.base + '/mcp'}, form=True)
        self.check('revoked_refresh_denied', status == 400)
        return self.results
