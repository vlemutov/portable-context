import ast
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

try:
    import httpx2 as httpx
    import jwt
    import mcp
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    httpx = None

from portable_context.core import Store
from portable_context.mcp_server import build_server
from portable_context.oauth import OAuthConfig, PocketIDVerifier, http_app
from portable_context.permissions import TOOLS, tool_scope, CapabilityPolicy, READ, WRITE, ADMIN


@unittest.skipUnless(httpx, 'Install .[mcp] for OAuth integration tests')
class OAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = OAuthConfig('https://memory.example', 'https://memory.example/mcp',
                                  'http://127.0.0.1:14112', 'owner')
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid='key-one', use='sig', alg='RS256')
        self.revoked = set()
        self.provider_status = 200
        self.identity_subject = 'owner'
        self.requests = []

        def provider(request):
            self.requests.append(request)
            if self.provider_status != 200:
                return httpx.Response(self.provider_status, headers={'Location': 'https://untrusted.example'})
            if request.url.path == '/.well-known/jwks.json':
                return httpx.Response(200, json={'keys': [self.jwk]})
            if request.url.path == '/api/oidc/userinfo':
                if request.headers.get('authorization', '')[7:] in self.revoked:
                    return httpx.Response(401)
                return httpx.Response(200, json={'sub': self.identity_subject})
            return httpx.Response(404)
        self.verifier = PocketIDVerifier(self.config, transport=httpx.MockTransport(provider))

    def token(self, scopes='openid memory:read', *, header=None, **claims):
        payload = dict(iss=self.config.issuer, aud=[self.config.issuer, self.config.resource],
            sub='owner', client_id='client-a', scope=scopes, iat=int(time.time()),
            exp=int(time.time())+300, jti='synthetic-token')
        payload.update(claims)
        return jwt.encode(payload, self.key, algorithm='RS256',
                          headers={'kid': 'key-one', 'typ': 'at+jwt', **(header or {})})

    async def test_signature_claims_owner_scope_and_type(self):
        accepted = await self.verifier.verify_token(self.token())
        self.assertEqual(accepted.client_id, 'client-a')
        self.assertEqual(accepted.resource, self.config.resource)
        bad = [self.token(aud='https://other.example/mcp'), self.token(iss='https://other.example'),
            self.token(sub='another-owner'), self.token(exp=int(time.time())-10),
            self.token(iat=int(time.time())+600), self.token(client_id=''),
            self.token(header={'typ': 'JWT'}), self.token(header={'kid': 'unknown'}),
            self.token('memory:read'), self.token('openid memory:write'),
            self.token()[:-8]+'tampered', 'not-a-jwt']
        for token in bad:
            with self.subTest(case=bad.index(token)):
                self.assertIsNone(await self.verifier.verify_token(token))
        self.assertTrue(all(str(r.url).startswith(self.config.provider_origin) for r in self.requests))

    async def test_revocation_outage_redirect_and_subject_mismatch_fail_closed(self):
        token = self.token()
        self.assertIsNotNone(await self.verifier.verify_token(token))
        self.revoked.add(token)
        self.assertIsNone(await self.verifier.verify_token(token))
        self.revoked.clear()
        for status in [302, 500, 401]:
            self.provider_status = status
            self.assertIsNone(await self.verifier.verify_token(token))
        self.provider_status = 200
        self.identity_subject = 'someone-else'
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_all_tools_enforced_before_handler_and_recheck_after_http_auth(self):
        from types import SimpleNamespace
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.shared.exceptions import MCPError
        policy = CapabilityPolicy(self.store, self.verifier)
        called = []
        async def handler(ctx):
            called.append(ctx)
            return {}
        for scopes, allowed in [('openid memory:read', READ),
            ('openid memory:read memory:write', READ | WRITE),
            ('openid memory:read memory:write memory:admin', READ | WRITE | ADMIN)]:
            access = await self.verifier.verify_token(self.token(scopes))
            reset = auth_context_var.set(AuthenticatedUser(access))
            try:
                for name in READ | WRITE | ADMIN:
                    ctx = SimpleNamespace(method='tools/call', params={'name': name})
                    before = len(called)
                    if name in allowed:
                        await policy(ctx, handler)
                        self.assertEqual(len(called), before+1)
                    else:
                        with self.assertRaises(MCPError):
                            await policy(ctx, handler)
                        self.assertEqual(len(called), before)
                for method, params in [('a_future_method', {}), ('resources/read', {'uri': 'file:///private'})]:
                    with self.assertRaises(MCPError):
                        await policy(SimpleNamespace(method=method, params=params), handler)
                self.revoked.add(access.token)
                with self.assertRaises(MCPError):
                    await policy(SimpleNamespace(method='tools/call', params={'name': 'memory_status'}), handler)
            finally:
                auth_context_var.reset(reset)

    async def test_ec_and_ed25519_and_reject_symmetric_tokens(self):
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519
        for algorithm, key in [('ES256', ec.generate_private_key(ec.SECP256R1())),
                               ('EdDSA', ed25519.Ed25519PrivateKey.generate())]:
            jwk = json.loads(jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(key.public_key()))
            jwk.update(kid=algorithm, use='sig', alg=algorithm)
            self.jwk = jwk
            self.verifier._keys_at = 0
            claims = jwt.decode(self.token(), options={'verify_signature': False})
            token = jwt.encode(claims, key, algorithm=algorithm, headers={'kid': algorithm, 'typ': 'at+jwt'})
            self.assertIsNotNone(await self.verifier.verify_token(token))
        token = jwt.encode(claims, 'synthetic-secret-with-at-least-32-characters', algorithm='HS256',
                           headers={'kid': 'EdDSA', 'typ': 'at+jwt'})
        self.assertIsNone(await self.verifier.verify_token(token))

    def test_config_requires_https_and_owner(self):
        for fields in [dict(issuer='http://memory.example'), dict(owner_subject=''),
                       dict(provider_origin='http://192.168.1.1'),
                       dict(resource='https://other.example/mcp'),
                       dict(provider_origin='https://user:secret@example.org')]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                OAuthConfig(**{**vars(self.config), **fields})

    async def test_every_registered_tool_has_explicit_policy(self):
        server = build_server(self.store)
        names = {t.name for t in await server.list_tools()}
        self.assertEqual(names, TOOLS)
        tree = ast.parse(Path('src/portable_context/mcp_server.py').read_text(encoding='utf-8'))
        resources = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                     isinstance(n.func, ast.Attribute) and n.func.attr == 'resource']
        self.assertEqual(len(resources), 2, 'New resources need capability classification')

    def test_path_and_job_sensitive_permissions(self):
        from portable_context.workspace import Workspace
        ws = Workspace(self.store)
        ws.register_source('host', 'Host', 'events', 'test')
        job = ws.stage_records('host', [{'source': 'test', 'instance': 'test',
            'conversation_id': 'one', 'message_id': '1', 'role': 'user', 'text': 'Selected data'}], 'request-one')
        cases = [('store_attachment', {}, 'memory:write'),
                 ('store_attachment', {'server_path': ''}, 'memory:admin'),
                 ('storage_report', {}, 'memory:read'),
                 ('storage_report', {'refresh_s3': True}, 'memory:admin'),
                 ('run_import', {'job_id': job['id']}, 'memory:write'),
                 ('run_import', {'job_id': 'unknown'}, 'memory:admin'),
                 ('connect_source', {'location': '/private'}, 'memory:admin'),
                 ('connect_source', {'source_id': 'another'}, 'memory:write'),
                 ('a_future_tool', {}, None)]
        for name, args, expected in cases:
            self.assertEqual(tool_scope(name, args, self.store), expected)

    async def test_real_mcp_http_permissions_discovery_and_live_revocation(self):
        server = build_server(self.store, oauth_config=self.config, token_verifier=self.verifier)
        app = http_app(server, self.config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.config.issuer,
                headers={'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}) as client:
                reply = await client.post('/mcp', json={})
                self.assertEqual(reply.status_code, 401)
                self.assertIn('resource_metadata=', reply.headers['www-authenticate'])
                self.assertIn('scope="openid memory:read"', reply.headers['www-authenticate'])
                metadata_url = reply.headers['www-authenticate'].split('resource_metadata="')[1].split('"')[0]
                metadata = (await client.get(metadata_url)).json()
                self.assertEqual(metadata['resource'], self.config.resource)
                self.assertEqual(metadata['authorization_servers'], [self.config.issuer])
                self.assertIn('memory:write', metadata['scopes_supported'])
                self.assertIn('memory:admin', metadata['scopes_supported'])

                read_token = self.token()
                async def rpc(method, params=None, token=read_token, **headers):
                    return await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1,
                        'method': method, 'params': params or {}},
                        headers={'Authorization': 'Bearer '+token, **headers})

                initialized = await rpc('initialize', {'protocolVersion': '2025-11-25',
                    'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
                self.assertEqual(initialized.status_code, 200, initialized.text)
                self.assertNotIn('mcp-session-id', initialized.headers)
                result = await rpc('tools/call', {'name': 'memory_status'})
                self.assertIn('workspace_id', result.text)
                resource = await rpc('resources/read', {'uri': 'portable-context://guide'})
                self.assertIn('start_import', resource.text)
                denied = await rpc('tools/call', {'name': 'create_project', 'arguments':
                    {'identifier': 'forbidden', 'title': 'Must not exist'}})
                self.assertEqual(denied.json()['error']['code'], -32003)
                self.assertNotIn('forbidden', (await rpc('tools/call', {'name': 'list_projects'})).text)
                writer = self.token('openid memory:read memory:write', client_id='writer')
                created = await rpc('tools/call', {'name': 'create_project', 'arguments':
                    {'identifier': 'permitted', 'title': 'Selected project'}}, writer)
                self.assertNotIn('error', created.json(), created.text)
                self.assertIn('Selected project', created.text)
                for name, args in [('store_attachment', {'server_path': '/private'}),
                                   ('set_capture_policy', {}), ('a_future_tool', {})]:
                    denied = await rpc('tools/call', {'name': name, 'arguments': args}, writer)
                    self.assertEqual(denied.json()['error']['code'], -32003)
                forbidden_host = await rpc('tools/call', {'name': 'memory_status'}, Host='evil.example')
                self.assertEqual(forbidden_host.status_code, 421)
                self.revoked.add(read_token)
                revoked = await rpc('tools/call', {'name': 'memory_status'})
                self.assertEqual(revoked.status_code, 401)
                self.assertEqual((await rpc('tools/call', {'name': 'list_projects'}, writer)).status_code, 200)


if __name__ == '__main__':
    unittest.main()
