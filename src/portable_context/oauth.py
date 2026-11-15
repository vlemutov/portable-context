"""Pocket ID resource-server integration. Never issues tokens or accepts login secrets.

The issuer is public HTTPS; provider calls use a fixed, owner-configured HTTPS
origin or the co-located provider's loopback HTTP port. Token-supplied URLs are
never fetched. Userinfo checks revocation online, in addition to JWT validation.
"""
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


def _origin(value, *, loopback=False):
    if not isinstance(value, str):
        raise ValueError('OAuth URLs must be strings')
    url = urlsplit(value)
    if (url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
            or not url.hostname or url.port == 0):
        raise ValueError('Expected an OAuth origin without credentials, path, query or fragment')
    if url.scheme != 'https' and not (loopback and url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1')):
        raise ValueError('OAuth requires HTTPS (provider-only HTTP may use an explicit loopback address)')
    return value.rstrip('/')


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    resource: str
    provider_origin: str
    owner_subject: str

    def __post_init__(self):
        issuer = _origin(self.issuer)
        if issuer != self.issuer:
            raise ValueError('Issuer must be canonical, with no trailing slash')
        _origin(self.provider_origin, loopback=True)
        if self.resource != issuer + '/mcp':
            raise ValueError('This deployment requires the MCP resource at issuer + /mcp')
        if not isinstance(self.owner_subject, str) or not self.owner_subject.strip():
            raise ValueError('Configure the owner subject before exposing OAuth MCP')

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text(encoding='utf-8')))


class PocketIDVerifier:
    """No positive token cache: every request observes provider-side revocation.

Public keys alone are cached for 5 minutes, with bounded refresh on an unknown
    key. Only explicitly supported asymmetric signing algorithms are accepted.
"""
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport
        self._keys = []
        self._keys_at = 0.0
        self._lock = asyncio.Lock()

    async def _json(self, path, token=None):
        import httpx2 as httpx
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        async with httpx.AsyncClient(transport=self.transport, timeout=5, trust_env=False,
                                    follow_redirects=False) as client:
            async with client.stream('GET', self.config.provider_origin.rstrip('/') + path, headers=headers) as response:
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 262144:
                        return None
                value = json.loads(body)
                return value if isinstance(value, dict) else None

    async def _key(self, kid):
        async with self._lock:
            now = time.monotonic()
            matches = [key for key in self._keys if isinstance(key, dict) and key.get('kid') == kid]
            if now - self._keys_at > 300 or (not matches and now - self._keys_at > 5):
                self._keys_at = now
                document = await self._json('/.well-known/jwks.json')
                self._keys = document.get('keys', []) if document else []
                if not isinstance(self._keys, list) or len(self._keys) > 32:
                    self._keys = []
                matches = [key for key in self._keys if isinstance(key, dict) and key.get('kid') == kid]
            return matches[0] if len(matches) == 1 else None

    async def verify_token(self, token):
        import httpx2 as httpx
        import jwt
        from mcp.server.auth.provider import AccessToken
        if not isinstance(token, str) or len(token) > 16384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get('alg')
            key_types = {'RS256': 'RSA', 'ES256': 'EC', 'EdDSA': 'OKP'}
            if (algorithm not in key_types or not isinstance(header.get('kid'), str)
                    or header.get('typ') not in ('at+jwt', 'application/at+jwt')):
                return None
            key = await self._key(header['kid'])
            if (not key or key.get('kty') != key_types[algorithm] or key.get('use', 'sig') != 'sig'
                    or key.get('alg', algorithm) != algorithm):
                return None
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm=algorithm).key,
                algorithms=[algorithm], audience=self.config.resource, issuer=self.config.issuer,
                options={'require': ['iss', 'aud', 'sub', 'exp', 'iat', 'client_id', 'jti']})
            subject, client_id = claims['sub'], claims['client_id']
            if subject != self.config.owner_subject or not isinstance(client_id, str) or not client_id:
                return None
            scopes = claims.get('scope')
            if not isinstance(scopes, str):
                return None
            scopes = scopes.split()
            if 'openid' not in scopes or 'memory:read' not in scopes:
                return None
            identity = await self._json('/api/oidc/userinfo', token)
            if not identity or identity.get('sub') != subject or claims['exp'] <= time.time():
                return None
            return AccessToken(token=token, client_id=client_id, subject=subject, scopes=scopes,
                expires_at=claims['exp'], resource=self.config.resource, claims={'iss': self.config.issuer})
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError):
            # Do not leak bearer values, provider responses or exception URLs.
            return None


def server_options(store, config, verifier=None):
    from mcp.server.auth.settings import AuthSettings
    from .permissions import CapabilityPolicy
    verifier = verifier or PocketIDVerifier(config)
    return dict(auth=AuthSettings(issuer_url=config.issuer, resource_server_url=config.resource,
                required_scopes=['openid', 'memory:read'], validate_token_resource=True),
                token_verifier=verifier, middleware=[CapabilityPolicy(store, verifier)])


class MinimumScopeChallenge:
    """Advertise a safe initial grant, distinct from all supported permissions."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def with_scopes(message):
            if message['type'] == 'http.response.start' and message['status'] in (401, 403):
                headers = []
                for key, value in message.get('headers', []):
                    if key.lower() == b'www-authenticate' and b'scope=' not in value:
                        value += b', scope="openid memory:read"'
                    headers.append((key, value))
                message = {**message, 'headers': headers}
            await send(message)
        await self.app(scope, receive, with_scopes)


def http_app(server, config):
    from mcp.server.auth.routes import create_protected_resource_routes
    from mcp.server.transport_security import TransportSecuritySettings
    from .permissions import SCOPES
    url = urlsplit(config.resource)
    app = server.streamable_http_app(stateless_http=True, json_response=True,
        max_request_body_size=16*1024*1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[url.netloc], allowed_origins=[config.issuer]))
    # Supported permissions differ from the read-only minimum required to connect.
    routes = create_protected_resource_routes(server.settings.auth.resource_server_url,
        [server.settings.auth.issuer_url], scopes_supported=['openid', 'offline_access', *SCOPES],
        resource_name='Portable Context')
    paths = {route.path for route in routes}
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, 'path', None) not in paths] + routes
    app.add_middleware(MinimumScopeChallenge)
    return app
