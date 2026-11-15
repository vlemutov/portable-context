import json
import http.client
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import server


class TokenBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk['kid'] = 'test'

    def setUp(self):
        server.STATE.clear()
        server.STATE.update(origin='https://test.example', owner='synthetic-owner', ready=True)
        self.claims = {'iss': 'https://test.example', 'aud': 'https://test.example/mcp',
            'sub': 'synthetic-owner', 'exp': int(time.time()) + 60,
            'client_id': 'pc-lab-client', 'scope': 'openid memory:read'}

    def upstream(self, method, path, *args, **kwargs):
        data = {'keys': [self.jwk]} if path.endswith('jwks.json') else {'sub': 'synthetic-owner'}
        return 200, [], json.dumps(data).encode()

    def token(self, **claims):
        return 'Bearer ' + jwt.encode({**self.claims, **claims}, self.key, algorithm='RS256',
            headers={'kid': 'test', 'typ': 'at+jwt'})

    def test_signature_owner_audience_scope_client_and_expiry(self):
        with patch.object(server, 'upstream', self.upstream):
            self.assertTrue(server.authorized(self.token(), 'memory:read'))
            self.assertFalse(server.authorized(self.token(), 'memory:write'))
            for changes in ({'aud': 'https://other.example/mcp'}, {'iss': 'https://other.example'},
                    {'sub': 'another-owner'}, {'exp': 1}, {'client_id': 'other-client'}, {'scope': []}):
                self.assertFalse(server.authorized(self.token(**changes), 'memory:read'))
            self.assertFalse(server.authorized('Bearer invalid', 'memory:read'))

    def test_revocation_checked_online(self):
        def revoked(method, path, *args, **kwargs):
            return self.upstream(method, path) if path.endswith('jwks.json') else (401, [], b'{}')
        with patch.object(server, 'upstream', revoked):
            self.assertFalse(server.authorized(self.token(), 'memory:read'))


class ProxyConnectionTests(unittest.TestCase):
    def test_rejected_post_does_not_poison_next_request(self):
        app = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        worker = threading.Thread(target=app.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection('127.0.0.1', app.server_port)
        try:
            connection.request('POST', '/_diag/configure', b'{"origin":"https://test.example"}')
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            self.assertTrue(response.will_close)
            connection.request('GET', '/health')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['fixture'], 'portable-context-transport')
        finally:
            connection.close()
            app.shutdown()
            app.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
