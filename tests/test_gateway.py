"""Actual Caddy -> mTLS -> origin tests, not a mocked proxy implementation."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[1] / 'deploy/gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from cloud_gateway import container_request, public_origin


class GatewayInputTests(unittest.TestCase):
    def test_cloud_request_keeps_memory_and_private_key_out_of_container_spec(self):
        payload = container_request('00000000-0000-0000-0000-000000000001', 'memory-edge',
            'owner.cr.cloud.ru/edge@sha256:' + 'a' * 64, config=b'config', certificate=b'cert', ca=b'ca',
            key_secret='edge-client-g1')
        template = payload['template']
        self.assertNotIn('volumes', template)
        self.assertEqual(template['scaling']['maxInstanceCount'], 1)
        container = template['containers'][0]
        self.assertNotIn('\n', container['args'][0])
        key = next(e for e in container['env'] if e['name'] == 'PC_EDGE_KEY_B64')
        self.assertEqual(key, {'name': 'PC_EDGE_KEY_B64', 'type': 'secret', 'value': 'edge-client-g1'})

    def test_provider_bare_host_normalized_but_insecure_or_ambiguous_urls_rejected(self):
        def response(uri):
            return {'configuration': {'ingress': {'publicUri': uri}}}
        self.assertEqual(public_origin(response('memory.containerapps.ru')), 'https://memory.containerapps.ru')
        for uri in ['http://memory.containerapps.ru', 'https://user:pass@example.com',
                    '//example.com', 'https://example.com/mcp', 'https://example.com?token=secret']:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                public_origin(response(uri))

    def test_untrusted_configuration_cannot_inject_caddy_directives(self):
        for name in ['foo\nrespond 200', 'https://example.com', '*.example.com', '-bad.example', 'a..b']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                gateway.edge_config(name, '127.0.0.1')
        for ip in ['localhost', '127.0.0.1:8080', '127.0.0.1\nadmin :2019']:
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                gateway.edge_config('memory.example', ip)

    def test_generation_is_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'keys'
            gateway.issue_credentials(root, 'memory.example')
            key = (root / 'edge-key.pem').read_bytes()
            with self.assertRaises(ValueError):
                gateway.issue_credentials(root, 'memory.example')
            self.assertEqual(key, (root / 'edge-key.pem').read_bytes())


@unittest.skipUnless(os.environ.get('PC_TEST_CADDY'), 'Set PC_TEST_CADDY to a checksum-verified Caddy binary')
class GatewayIntegrationTests(unittest.TestCase):
    identity_enabled = False
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.secrets = cls.root / 'keys'
        gateway.issue_credentials(cls.secrets, 'memory.example')
        cls.calls = []

        class Application(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                cls.calls.append(body)
                self.send_response(503 if self.path == '/mutation-fails' else 200)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/disconnect':
                    self.close_connection = True
                    return
                if self.path == '/mcp' and self.headers.get('Authorization') != 'Bearer test-credential':
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Bearer resource_metadata="https://memory.example/.well-known/oauth-protected-resource"')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('X-Test-Backend', str(self.server.server_port))
                self.end_headers()
                self.wfile.write(json.dumps(dict(self.headers)).encode())

            def log_message(self, *_):
                pass

        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), Application)
        cls.addClassCleanup(cls.app.server_close)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.app.shutdown)
        identity_port = None
        if cls.identity_enabled:
            cls.identity = ThreadingHTTPServer(('127.0.0.1', 0), Application)
            cls.addClassCleanup(cls.identity.server_close)
            threading.Thread(target=cls.identity.serve_forever, daemon=True).start()
            cls.addClassCleanup(cls.identity.shutdown)
            identity_port = cls.identity.server_port

        def free_port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]

        cls.origin_port = free_port()
        cls.edge_port = free_port()
        origin = gateway.origin_config('memory.example', cls.secrets, cls.origin_port, cls.app.server_port,
                                       identity_port=identity_port, revision='origin-test')
        edge = gateway.edge_config('memory.example', '127.0.0.1', cls.secrets, cls.edge_port, cls.origin_port, revision='edge-test')
        for name, config, listener in [('origin', origin, cls.origin_port), ('edge', edge, cls.edge_port)]:
            path = cls.root / (name + '.Caddyfile')
            # Synthetic fixture only: retain startup errors for platform diagnosis.
            # These tests use TCP only. Reserving a free TCP port does not reserve
            # its UDP counterpart (QUIC), including excluded Windows UDP ports.
            config = config.replace('persist_config off', 'persist_config off\n    servers {\n        protocols h1 h2\n    }')
            path.write_text(config.replace('output discard', 'output stderr'), encoding='utf-8')
            subprocess.run([os.environ['PC_TEST_CADDY'], 'validate', '--adapter', 'caddyfile', '--config', str(path)],
                           capture_output=True, check=True)
            startup_log = (cls.root / (name + '.startup.log')).open('w+b')
            cls.addClassCleanup(startup_log.close)
            process = subprocess.Popen([os.environ['PC_TEST_CADDY'], 'run', '--adapter', 'caddyfile', '--config', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=startup_log)
            def stop(process=process):
                process.terminate()
                process.wait(timeout=10)
            cls.addClassCleanup(stop)
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', listener), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() > deadline or process.poll() is not None:
                        startup_log.seek(0)
                        raise RuntimeError('Caddy did not start: '+startup_log.read().decode(errors='replace')[-3000:])
                    time.sleep(.05)

    def request(self, path, *, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.edge_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body, {'Host': 'memory.example', **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.headers), response.read()

    def test_preserves_auth_challenge_and_credential(self):
        status, headers, _ = self.request('/mcp')
        self.assertEqual(status, 401)
        self.assertNotIn('Alt-Svc', headers)
        self.assertIn('resource_metadata=', headers['Www-Authenticate'])
        status, _, body = self.request('/mcp', headers={'Authorization': 'Bearer test-credential'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['Authorization'], 'Bearer test-credential')

    def test_rotation_probe_precedes_identity_and_application_routes(self):
        status, headers, body = self.request('/_pc/transport')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'origin-test')
        self.assertEqual(headers['X-Pc-Edge-Revision'], 'edge-test')
        self.assertEqual(headers['X-Pc-Origin-Revision'], 'origin-test')

    def test_proxy_error_is_marked_without_leaking_request_or_exception(self):
        status, headers, body = self.request('/disconnect', headers={'Authorization':'Bearer NEVER-LOG-THIS'})
        self.assertEqual(status, 502)
        self.assertEqual(headers['X-Pc-Failure-Layer'], 'origin')
        self.assertEqual(headers['X-Pc-Edge-Revision'], 'edge-test')
        self.assertEqual(headers['X-Pc-Origin-Revision'], 'origin-test')
        self.assertEqual(body, b'Portable Context transport error')

    def test_edge_connection_failure_is_distinct_from_origin_failure(self):
        with socket.socket() as unavailable, socket.socket() as reserved:
            unavailable.bind(('127.0.0.1', 0))  # Bound but not listening.
            reserved.bind(('127.0.0.1', 0));listen = reserved.getsockname()[1]
            config = gateway.edge_config('memory.example', '127.0.0.1', self.secrets,
                listen, unavailable.getsockname()[1], revision='edge-test')
            path = self.root/'failing-edge.Caddyfile';path.write_text(config)
            reserved.close()
            process = subprocess.Popen([os.environ['PC_TEST_CADDY'], 'run', '--adapter', 'caddyfile', '--config', str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+10
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1',listen), timeout=.2): break
                    except OSError:
                        if time.monotonic()>deadline or process.poll() is not None: raise
                        time.sleep(.05)
                conn = http.client.HTTPConnection('127.0.0.1',listen,timeout=5)
                try:
                    conn.request('GET','/_pc/transport',headers={'Host':'memory.example'})
                    response = conn.getresponse();body = response.read()
                    self.assertEqual(response.status,502)
                    self.assertEqual(response.getheader('X-PC-Failure-Layer'),'edge')
                    self.assertEqual(response.getheader('X-PC-Edge-Revision'),'edge-test')
                    self.assertIsNone(response.getheader('X-PC-Origin-Revision'))
                    self.assertEqual(body,b'Portable Context transport error')
                finally: conn.close()
            finally: process.terminate();process.wait(timeout=10)

    def test_drops_spoofed_proxy_headers(self):
        status, _, body = self.request('/headers', headers={'Forwarded': 'host=evil', 'X-Forwarded-For': '1.2.3.4',
            'X-Real-IP': '1.2.3.4', 'X-Forwarded-Host': 'evil', 'X-Forwarded-Proto': 'http'})
        headers = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Host'], 'memory.example')
        self.assertEqual(headers['X-Forwarded-Proto'], 'https')
        for name in ['Forwarded', 'X-Forwarded-For', 'X-Real-Ip']:
            self.assertNotIn(name, headers)
        self.assertEqual(self.request('/headers', headers={'Host': 'evil.example'})[0], 404)

    def test_body_transmitted_exactly_and_failed_mutation_not_retried(self):
        body = b'\x00\xff' * 4096
        before = len(self.calls)
        status, headers, returned = self.request('/mutation-fails', method='POST', body=body)
        self.assertEqual(status, 503)
        self.assertNotIn('X-Pc-Failure-Layer', headers)
        self.assertEqual(body, returned)
        self.assertEqual(len(self.calls), before + 1)

    def test_origin_refuses_missing_and_unrelated_client_certificate(self):
        for unrelated in [False, True]:
            context = ssl.create_default_context(cafile=str(self.secrets / 'origin-ca.pem'))
            if unrelated:
                other = self.root / 'unrelated'
                gateway.issue_credentials(other, 'memory.example')
                context.load_cert_chain(other / 'edge.pem', other / 'edge-key.pem')
            with self.subTest(unrelated=unrelated), self.assertRaises((ssl.SSLError, ConnectionError)):
                with socket.create_connection(('127.0.0.1', self.origin_port), timeout=3) as sock:
                    with context.wrap_socket(sock, server_hostname='memory.example') as conn:
                        conn.sendall(b'GET / HTTP/1.1\r\nHost: memory.example\r\nConnection: close\r\n\r\n')
                        if not conn.recv(1024):
                            raise ConnectionError('TLS rejected')


class OAuthGatewayIntegrationTests(GatewayIntegrationTests):
    identity_enabled = True

    def test_identity_routing_blocks_public_setup_and_bootstrap_keys(self):
        for path in ['/authorize', '/.well-known/openid-configuration', '/api/oidc/token']:
            status, headers, body = self.request(path, headers={'X-API-Key': 'must-never-reach-provider'})
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Test-Backend'], str(self.identity.server_port))
            self.assertNotIn('must-never-reach-provider', body.decode())
        status, headers, _ = self.request('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(status, 200)
        self.assertEqual(headers['X-Test-Backend'], str(self.app.server_port))
        for path in ['/setup', '/api/signup', '/api/signup/setup']:
            self.assertEqual(self.request(path)[0], 404)
            self.assertEqual(self.request(path, method='POST', body=b'{}')[0], 404)


if __name__ == '__main__':
    unittest.main()
