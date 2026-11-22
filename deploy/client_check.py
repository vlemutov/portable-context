"""Interactive HTTPS acceptance from the owner's computer. Tokens live only in this process."""
import base64
import hashlib
import json
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from setup import read


PROJECT = 'Portable Context HTTPS check'
FILE = 'portable-context-check.txt'
DATA = 'Portable Context\r\nПроверка HTTPS.\r\n'.encode()
PREVIEW = f'Создаст тестовый проект «{PROJECT}», диалог из двух сообщений и {FILE} ({len(DATA)} байт). Они останутся в памяти.'
CALLBACK = 'http://127.0.0.1:14881/callback'


def authorize(origin, resource, write):
    import httpx2
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    code = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal code
            path = urlsplit(self.path)
            params = parse_qs(path.query)
            valid = (self.headers.get('Host') == urlsplit(CALLBACK).netloc and path.path == '/callback'
                     and params.get('state') == [state] and params.get('iss', [origin]) == [origin])
            if not valid:
                self.send_error(400)
                return
            code = params.get('code', [''])[0]
            self.send_response(200)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(b'Return to Portable Context.')

    client_id = 'portable-context-desktop'
    with httpx2.Client(timeout=30, follow_redirects=False) as client:
        discovery = client.get(origin + '/.well-known/openid-configuration')
        if discovery.status_code != 200:
            raise ValueError('OAuth discovery failed (HTTP %s)' % discovery.status_code)
        metadata = discovery.json()
        if metadata.get('issuer') != origin or any(
            urlsplit(metadata[k])[:2] != urlsplit(origin)[:2] for k in ('authorization_endpoint', 'token_endpoint')
        ):
            raise ValueError('Unexpected OAuth issuer or endpoint in discovery')
        query = urlencode(dict(response_type='code', client_id=client_id, redirect_uri=CALLBACK,
                               scope='openid memory:read' + (' memory:write' if write else ''),
                               resource=resource, state=state, code_challenge=challenge, code_challenge_method='S256'))
        with HTTPServer(('127.0.0.1', urlsplit(CALLBACK).port), Handler) as callback:
            callback.timeout = 1
            if not webbrowser.open(metadata['authorization_endpoint'] + '?' + query):
                raise ValueError('Open the check from a desktop with a browser')
            deadline = time.monotonic() + 300
            while code is None and time.monotonic() < deadline:
                callback.handle_request()
        if not code:
            raise ValueError('OAuth login expired or was declined')
        token = client.post(metadata['token_endpoint'], data=dict(grant_type='authorization_code',
                            code=code, redirect_uri=CALLBACK, client_id=client_id,
                            code_verifier=verifier, resource=resource))
        if token.status_code != 200:
            raise ValueError('OAuth token exchange failed (HTTP %s)' % token.status_code)
        return token.json()['access_token']


async def roundtrip(call, workspace):
    from portable_context.core import key
    instance = 'pc-client-check-' + workspace
    project_id = key('memory-project', instance)
    sha = hashlib.sha256(DATA).hexdigest()
    records = [dict(source='acceptance-test', instance=instance, conversation_id='https-file',
                    message_id=str(i), role=role, text=text, parent_id=None if i == 1 else '1',
                    title=PROJECT, memory_project=dict(id=instance, name=PROJECT, basis='user_selection'),
                    attachments=[dict(name=FILE, role='input', purpose='HTTPS round trip', status='current',
                                      sha256=sha)] if i == 1 else [])
               for i, role, text in [(1, 'user', 'Roundtrip: retain the attached test file.'),
                                     (2, 'assistant', 'Roundtrip: this is a synthetic attachment.')]]
    await call('connect_source', dict(source_id=instance, label=PROJECT, format='events', instance=instance))
    jobs = []
    snapshots = []
    for suffix in ('first', str(uuid.uuid4())):
        staged = await call('stage_records', dict(source_id=instance, records=records,
                            request_id=instance + '-' + suffix, executor={'client': 'Portable Context client-check'}))
        job = await call('run_import', {'job_id': staged['id']})
        if job['status'] != 'completed':
            raise ValueError('Import did not finish. Inspect job ' + staged['id'])
        jobs.append({'id': job['id'], 'hash': job['specification']['hash'], 'counts': job['import_counts']})
        projects = await call('list_projects', {'query': PROJECT})
        messages = await call('search_messages', {'instance': instance, 'query': 'Roundtrip'})
        if len(messages) != 2:
            raise ValueError('Unexpected number of messages in test dialogue')
        linked = []
        for item in messages:
            msg = await call('read_message', {'message_key': item['id']})
            sent = next((r for r in records if r['message_id'] == msg['message_id']), {})
            if any(msg.get(k) != sent.get(k) for k in ('instance','conversation_id','role','text','parent_id')):
                raise ValueError('Read-back message differs from fixture')
            linked.append((msg['message_id'], msg['key'], msg['revision'], len(msg['history'])))
        if not any(p['id'] == project_id for p in projects):
            raise ValueError('Test project missing')
        file, = await call('list_files', {'project_id': project_id, 'query': FILE})
        ref, = file['occurrences']
        await call('store_attachment', dict(occurrence_id=ref['id'], expected_sha256=sha,
                            content_base64=base64.b64encode(DATA).decode()))
        passport = await call('read_file', {'file_id': file['id']})
        ref, = passport['occurrences']
        version, = passport['versions']
        linked.sort()
        if ([m[0] for m in linked] != ['1', '2'] or any(m[3] != 1 for m in linked)
                or ref['message_key'] != linked[0][1] or version['sha256'] != sha or ref['sha256'] != sha):
            raise ValueError('Attachment version or source link differs')
        returned = await call('read_file_bytes', {'sha256': sha})
        data = base64.b64decode(returned['content_base64'], validate=True)
        if data != DATA or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('File bytes differ')
        snapshots.append((linked, file['id'], ref['id']))
    if (snapshots[0] != snapshots[1] or jobs[0]['id'] == jobs[1]['id'] or jobs[0]['hash'] != jobs[1]['hash']
            or jobs[1]['counts'] != {'inserted': 0, 'revised': 0, 'unchanged': 2}):
        raise ValueError('Repeat import did not preserve fixture identity')
    return {'project_id': project_id, 'imports': jobs, 'file_sha256': sha, 'bytes': len(DATA)}


async def verify(call, workspace, write):
    status = await call('memory_status', {})
    if status['workspace_id'] != workspace:
        raise ValueError('Unexpected workspace_id; check the client endpoint')
    projects = await call('list_projects', {'limit': 30})
    result = {'workspace_id': workspace, 'projects_read': len(projects), 'stats_before': status['stats']}
    if write:
        result['roundtrip'] = await roundtrip(call, workspace)
        result['stats_after'] = (await call('memory_status', {}))['stats']
    return result


async def session_check(session, workspace, write):
    async def call(tool, arguments):
        reply = await session.call_tool(tool, arguments)
        if reply.is_error:
            raise ValueError('MCP tool failed: ' + tool)
        value = reply.structured_content
        if value is None:
            value = json.loads(reply.content[0].text)
        return value.get('result', value) if isinstance(value, dict) else value
    return await verify(call, workspace, write)


async def run(state, write=False):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    receipt = read(state/'https-installed.json')
    origin = receipt['origin']
    resource = receipt['mcp_url']
    if (not origin.startswith('https://') or origin != 'https://' + str(urlsplit(origin).hostname)
            or resource != origin + '/mcp' or not receipt.get('workspace_id')):
        raise ValueError('Invalid HTTPS installation receipt')
    token = authorize(origin, resource, write)
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}, timeout=120) as client:
        async with streamable_http_client(resource, http_client=client) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                return await session_check(session, receipt['workspace_id'], write)
