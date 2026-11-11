"""Private loopback bootstrap for the tested Pocket ID v2.19.2 API.

Run before routing the public origin. Keep its receipt and key outside the repo.
Never print API responses: client credentials and login links can be included.
"""
import json
from pathlib import Path
import urllib.error
import urllib.request

VERSION = '2.19.2'
LINUX_AMD64_SHA256 = '85881ea87d78710c34f127b2d849d27dd7e69963723d9e590f66de26e87fc4fb'
OWNER_CODE_TTL = '20m'
CODEX_CALLBACK = 'http://127.0.0.1:14881/callback'
CLAUDE_CODE_CALLBACK = 'http://localhost:14881/callback'
PERMISSIONS = {'memory:read': 'Read memory', 'memory:write': 'Import and enrich memory',
               'memory:admin': 'Administer memory and server sources'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Bootstrap:
    def __init__(self, port, key):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('Invalid loopback port')
        self.base = f'http://127.0.0.1:{port}'
        self.key = key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, method=None):
        if not path.startswith('/api/') or '?' in path or '#' in path:
            raise ValueError('Invalid bootstrap API path')
        req = urllib.request.Request(self.base+path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={'Content-Type': 'application/json', 'X-API-Key': self.key})
        try:
            with self.opener.open(req, timeout=10) as response:
                body = response.read(1024*1024)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Pocket ID bootstrap HTTP {error.code}; response suppressed') from None

    def create_owner_login_code(self, owner_subject):
        """Return a code accepted by v2.19.2's no-email login form.

        TTLs <=15m produce six characters, but the form requires twelve when
        anonymous email login is disabled. Keep email login disabled and request
        a bounded 20m code; validate before handing it to the owner.
        """
        from uuid import UUID
        owner = str(UUID(owner_subject))
        result = self.request('/api/users/'+owner+'/one-time-access-token',
                              {'ttl': OWNER_CODE_TTL})
        code = result.get('token')
        if not isinstance(code, str) or len(code) != 12 or not code.isascii() or not code.isalnum():
            raise RuntimeError('Provider returned an incompatible owner login code; value suppressed')
        return code

    def enable_claude_code(self, receipt, before_change):
        """Explicit additive migration for the owned desktop client; no grant reset.

        Pocket ID's PUT replaces client settings, so carry forward the complete
        current DTO. Never rebuild an existing client from bootstrap defaults.
        before_change must create the owner's identity backup before the write.
        """
        client_id = 'portable-context-desktop'
        if client_id not in receipt.get('clients', []):
            raise ValueError('Desktop client is not owned by this bootstrap receipt')
        path = '/api/oidc/clients/'+client_id
        current = self.request(path)
        if (current.get('id') != client_id or current.get('clientType', 'standard') != 'standard'
                or current.get('isPublic') is not True or current.get('pkceEnabled') is not True
                or current.get('skipConsent') is not False):
            raise ValueError('Expected the public PKCE desktop client with consent; inspect owner settings')
        callbacks = current.get('callbackURLs')
        if not isinstance(callbacks, list) or CODEX_CALLBACK not in callbacks:
            raise ValueError('Desktop callback configuration changed; inspect owner settings')
        changed = CLAUDE_CODE_CALLBACK not in callbacks
        if changed:
            before_change()
            # Refuse overwriting an owner edit made while the checkpoint ran.
            if self.request(path) != current:
                raise ValueError('Client changed during backup; retry after reviewing owner settings')
            candidate = dict(current, callbackURLs=[*callbacks, CLAUDE_CODE_CALLBACK])
            self.request(path, candidate, 'PUT')
            verified = self.request(path)
            for key in ('callbackURLs', 'name', 'description', 'logoutCallbackURLs', 'backchannelLogoutURL',
                        'isPublic', 'pkceEnabled', 'requiresReauthentication', 'requiresPushedAuthorizationRequests',
                        'skipConsent', 'credentials', 'launchURL', 'hasLogo', 'hasDarkLogo', 'isGroupRestricted',
                        'accessTokenDurationMinutes', 'refreshTokenDurationMinutes', 'allowedUserGroups'):
                if verified.get(key) != candidate.get(key):
                    raise RuntimeError('OAuth client verification failed; inspect private identity backup')
        return {'client_id': client_id, 'callback_url': CLAUDE_CODE_CALLBACK,
                'changed': changed, 'codex_callback_preserved': True, 'permissions_changed': False}

    def configure(self, origin, receipt_path):
        """Create an owner and explicit grants, retaining IDs for repeatable setup.

        A pre-existing unclaimed user or API is never silently adopted. On a
        repeated run the recorded owner is verified, not replaced by username.
        """
        from gateway import hostname
        host = hostname(origin.removeprefix('https://'))
        if origin != 'https://' + host:
            raise ValueError('Use the canonical HTTPS origin')
        receipt_path = Path(receipt_path)
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {'origin': origin}
        if receipt['origin'] != origin:
            raise ValueError('Changing the OAuth origin requires explicit migration')

        def save():
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = receipt_path.with_suffix('.pending')
            temporary.write_text(json.dumps(receipt), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(receipt_path)

        if not receipt.get('owner_subject'):
            users = self.request('/api/users')['data']
            if any(user['username'] == 'portable-context-owner' for user in users):
                raise ValueError('Unclaimed owner already exists; recover its receipt privately')
            owner = self.request('/api/users', {'username': 'portable-context-owner',
                'firstName': 'Memory owner', 'isAdmin': True})
            receipt['owner_subject'] = owner['id']
            save()
        owner = self.request('/api/users/'+receipt['owner_subject'])
        if not owner.get('isAdmin'):
            raise ValueError('Recorded owner is not an administrator')
        if not receipt.get('api_id'):
            api = self.request('/api/apis', {'name': 'Portable Context', 'resource': origin+'/mcp'})
            receipt['api_id'] = api['id']
            save()
        api = self.request('/api/apis/'+receipt['api_id'])
        if api['resource'] != origin+'/mcp':
            raise ValueError('API receipt does not match this MCP resource')
        if not receipt.get('permissions_configured'):
            self.request('/api/apis/'+api['id']+'/permissions',
                {'permissions': [{'key': key, 'name': name} for key, name in PERMISSIONS.items()]}, 'PUT')
            receipt['permissions_configured'] = True
            save()
        api = self.request('/api/apis/'+api['id'])
        permissions = {p['key']: p['id'] for p in api['permissions']}
        for label, scopes in [('desktop', ['memory:read', 'memory:write']), ('admin', list(PERMISSIONS))]:
            client_id = 'portable-context-'+label
            if client_id not in receipt.get('clients', []):
                self.request('/api/oidc/clients', {'id': client_id,
                    'name': 'Portable Context '+label,
                    'callbackURLs': [CODEX_CALLBACK, CLAUDE_CODE_CALLBACK] if label == 'desktop' else [CODEX_CALLBACK],
                    'isPublic': True, 'pkceEnabled': True, 'skipConsent': False,
                    'accessTokenDurationMinutes': 10, 'refreshTokenDurationMinutes': 43200})
                receipt.setdefault('clients', []).append(client_id)
                save()
            if client_id in receipt.get('grants', []):
                continue  # Do not undo later owner choices on rerun.
            self.request('/api/apis/'+api['id']+'/clients/'+client_id,
                {'userDelegatedAccess': True, 'clientAccess': False,
                 'userDelegatedPermissionIds': [permissions[s] for s in scopes], 'clientPermissionIds': []}, 'PUT')
            receipt.setdefault('grants', []).append(client_id)
            save()
        return receipt
