"""Disposable transport fixture. Never deploy against a real identity or memory.

All files and identity state are intentionally ephemeral. No request logging.
The random DIAG_KEY is supplied at runtime, never baked into an image.
"""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import jwt

VERSION = Path(__file__).with_name('VERSION').read_text().strip()
KEY = os.environ.get('DIAG_KEY', '')
LIMIT = 8 * 1024 * 1024
LOCK = threading.Lock()
STATE = {}
FILES = {}


def upstream(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', 14112, timeout=15)
    try:
        conn.request(method, path, body, headers or {})
        res = conn.getresponse()
        return res.status, res.getheaders(), res.read(LIMIT + 1)
    finally:
        conn.close()


def admin(path, data=None, method=None):
    status, _, body = upstream(method or ('POST' if data is not None else 'GET'), path,
        json.dumps(data).encode() if data is not None else None,
        {'Content-Type': 'application/json', 'X-API-Key': STATE['admin_key']})
    if not 200 <= status < 300:
        raise RuntimeError('bootstrap HTTP ' + str(status))
    return json.loads(body) if body else {}


def configure(origin):
    with LOCK:
        if STATE.get('origin'):
            if STATE['origin'] != origin or not STATE.get('ready'):
                raise ValueError('configuration requires a fresh test container')
            return
        STATE.update(origin=origin, admin_key=secrets.token_urlsafe(48))
        directory = tempfile.mkdtemp(prefix='synthetic-identity-')
        env = {**os.environ, 'APP_URL': origin, 'HOST': '127.0.0.1', 'PORT': '14112',
            'ACTORS_HOST': '127.0.0.1', 'ACTORS_PORT': '14113',
            'UI_CONFIG_DISABLED': 'true', 'ENCRYPTION_KEY': secrets.token_urlsafe(48),
            'STATIC_API_KEY': STATE['admin_key'], 'ANALYTICS_DISABLED': 'true',
            'VERSION_CHECK_DISABLED': 'true', 'ICON_LIBRARY_URL': 'disabled',
            'ALLOW_USER_SIGNUPS': 'disabled', 'REQUIRE_USER_EMAIL': 'false',
            'APP_NAME': 'Disposable Portable Context diagnostic', 'LOG_LEVEL': 'error',
            'LOG_QUERY_ARGS': 'false', 'CIMD_URL_ALLOWLIST': '[]'}
        env.pop('DIAG_KEY', None)
        STATE['process'] = subprocess.Popen(['/usr/local/bin/pocket-id'], cwd=directory,
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if upstream('GET', '/.well-known/openid-configuration')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError('identity startup timeout')
        owner = admin('/api/users', {'username': 'pc-lab-owner', 'firstName': 'Synthetic owner',
            'isAdmin': True, 'email': 'owner@example.invalid', 'emailVerified': True})
        STATE['owner'] = owner['id']
        admin('/api/oidc/clients', {'id': 'pc-lab-client', 'name': 'Disposable transport test',
            'callbackURLs': ['http://localhost:14111/callback'], 'isPublic': True,
            'pkceEnabled': True, 'skipConsent': False})
        api = admin('/api/apis', {'name': 'Synthetic memory', 'resource': origin + '/mcp'})
        admin('/api/apis/' + api['id'] + '/permissions', {'permissions': [
            {'key': 'memory:read', 'name': 'Read synthetic data'},
            {'key': 'memory:write', 'name': 'Write synthetic data'}]}, 'PUT')
        api = admin('/api/apis/' + api['id'])
        admin('/api/apis/' + api['id'] + '/clients/pc-lab-client', {
            'userDelegatedAccess': True, 'clientAccess': False,
            'userDelegatedPermissionIds': [p['id'] for p in api['permissions']],
            'clientPermissionIds': []}, 'PUT')
        STATE['ready'] = True


def authorized(header, scope):
    if not STATE.get('ready') or not header.startswith('Bearer '):
        return False
    token = header[7:]
    try:
        status, _, raw = upstream('GET', '/.well-known/jwks.json')
        if status != 200:
            return False
        header_fields = jwt.get_unverified_header(token)
        kid = header_fields.get('kid')
        if header_fields.get('typ') not in ('at+jwt', 'application/at+jwt'):
            return False
        jwk = next(k for k in json.loads(raw)['keys'] if k['kid'] == kid)
        claims = jwt.decode(token, jwt.PyJWK.from_dict(jwk).key,
            algorithms=['RS256', 'ES256', 'EdDSA'], audience=STATE['origin'] + '/mcp',
            issuer=STATE['origin'], options={'require': ['exp', 'iss', 'aud', 'sub']})
        if (claims['sub'] != STATE['owner'] or claims.get('client_id') != 'pc-lab-client'
                or not isinstance(claims.get('scope'), str) or scope not in claims['scope'].split()):
            return False
        status, _, raw = upstream('GET', '/api/oidc/userinfo', headers={'Authorization': header})
        return status == 200 and json.loads(raw).get('sub') == STATE['owner']
    except (OSError, ValueError, KeyError, StopIteration, jwt.PyJWTError):
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, data, content_type='application/json', extra=()):
        raw = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('chunked uploads are outside this fixture')
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= LIMIT:
            raise ValueError('body limit exceeded')
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError('incomplete body')
        return body

    def dispatch(self):
        path = urlsplit(self.path).path
        if self.command in ('GET', 'HEAD') and path in ('/', '/health'):
            return self.reply(200, {'fixture': 'portable-context-transport', 'version': VERSION,
                'identity_ready': bool(STATE.get('ready'))})
        if path.startswith('/_diag/'):
            if not KEY or not hmac.compare_digest(self.headers.get('X-Diagnostic-Key', ''), KEY):
                return self.reply(401, {'error': 'diagnostic authorization required'})
            if path == '/_diag/configure' and self.command == 'POST':
                origin = json.loads(self.body())['origin']
                parsed = urlsplit(origin)
                if (parsed.scheme != 'https' or parsed.path or parsed.query or parsed.fragment
                        or parsed.netloc != self.headers.get('Host')
                        or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.netloc)):
                    raise ValueError('invalid HTTPS origin')
                configure(origin)
                return self.reply(200, {'configured': True})
            if path == '/_diag/login-code' and self.command == 'POST' and STATE.get('ready'):
                self.body()
                value = admin('/api/users/' + STATE['owner'] + '/one-time-access-token', {'ttl': '20m'})
                return self.reply(200, {'code': value['token'], 'owner': STATE['owner']})
            return self.reply(404, {'error': 'unknown diagnostic operation'})
        if path == '/events' or path.startswith('/files'):
            scope = 'memory:write' if self.command in ('POST', 'DELETE') else 'memory:read'
            if not authorized(self.headers.get('Authorization', ''), scope):
                self.close_connection = True
                return self.reply(401, {'error': 'valid delegated token required'})
            if path == '/events' and self.command == 'GET':
                seconds = min(70, max(2, int(parse_qs(urlsplit(self.path).query).get('seconds', ['6'])[0])))
                start = max(0, int(self.headers.get('Last-Event-ID', '0'))) + 1
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache, no-transform')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                for index in range(start, start + seconds):
                    self.wfile.write(f'id: {index}\nevent: tick\ndata: {index}\n\n'.encode())
                    self.wfile.flush()
                    time.sleep(1)
                return
            if path == '/files' and self.command == 'POST':
                data = self.body()
                digest = hashlib.sha256(data).hexdigest()
                with LOCK:
                    existed = digest in FILES
                    if not existed and sum(map(len, FILES.values())) + len(data) > 16 * 1024 * 1024:
                        return self.reply(507, {'error': 'fixture storage full'})
                    FILES[digest] = data
                return self.reply(200 if existed else 201, {'sha256': digest, 'bytes': len(data), 'duplicate': existed})
            digest = path.removeprefix('/files/')
            if re.fullmatch(r'[a-f0-9]{64}', digest):
                with LOCK:
                    data = FILES.get(digest)
                    if self.command == 'DELETE':
                        FILES.pop(digest, None)
                        return self.reply(200, {'deleted': True})
                if self.command == 'GET' and data is not None:
                    return self.reply(200, data, 'application/octet-stream')
            return self.reply(404, {'error': 'synthetic file not found'})
        allowed = (path in ('/authorize', '/.well-known/openid-configuration', '/.well-known/jwks.json',
            '/api/oidc/token', '/api/oidc/userinfo', '/api/oidc/users/me/authorized-clients/pc-lab-client')
            or re.fullmatch(r'/api/oidc/interactions/[A-Za-z0-9_-]+(?:/complete)?', path)
            or re.fullmatch(r'/api/one-time-access-token/[A-Za-z0-9]+', path))
        if not allowed or not STATE.get('ready'):
            return self.reply(404, {'error': 'route not exposed'})
        headers = {k: v for k, v in self.headers.items()
            if k.lower() in ('authorization', 'cookie', 'content-type', 'accept', 'user-agent')}
        headers.update({'Host': urlsplit(STATE['origin']).netloc, 'X-Forwarded-Proto': 'https'})
        status, response_headers, data = upstream(self.command, self.path, self.body(), headers)
        forwarded = [(k, v) for k, v in response_headers if k.lower() in ('set-cookie', 'location', 'www-authenticate')]
        kind = next((v for k, v in response_headers if k.lower() == 'content-type'), 'application/json')
        return self.reply(status, data, kind, forwarded)

    def do_GET(self):
        self.connection.settimeout(80)
        try:
            self.dispatch()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.close_connection = True
            # Do not log request paths, authorization codes, cookies or bodies.
            self.reply(500 if isinstance(exc, RuntimeError) else 400, {'error': type(exc).__name__})

    do_POST = do_GET
    do_DELETE = do_GET
    do_HEAD = do_GET


if __name__ == '__main__':
    if len(KEY) < 32:
        raise SystemExit('A runtime DIAG_KEY of at least 32 characters is required')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
