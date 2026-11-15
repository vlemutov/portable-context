"""Cloud.ru edge API adapter. Uses owner credentials from private deployment state.

All mutations are explicit. Errors deliberately omit response bodies: providers
can echo submitted environment values. No automatic retry of a mutation.
"""
from __future__ import annotations
import base64
import json
from pathlib import Path
import re
import random
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gateway import hostname

SERVICES = {'containers': 'https://containers.api.cloud.ru',
            'registry': 'https://ar.api.cloud.ru',
            'secrets': 'https://secretmanager.api.cloud.ru'}
CADDY_IMAGE = 'caddy:2.11.9-alpine@sha256:0bbb055bda6793c999bde78b1b5ce3e89f829fb031af2cf50056ea1246559fee'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CloudAPIError(RuntimeError):
    def __init__(self, status, method, reason='unclassified'):
        self.status = status
        self.reason = reason
        super().__init__(f'Cloud API HTTP {status}; operation {method}; reason {reason}; response body suppressed')


def error_reason(raw):
    """Return fixed labels only. Never expose arbitrary provider text or echoed secrets."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        value = {'message':raw.decode('utf-8','replace')} if isinstance(raw,bytes) else {'message':raw}
    if not isinstance(value, dict):
        return 'unclassified'
    messages = [value.get('message'), value.get('code')]
    if isinstance(value.get('error'), dict):
        messages += [value['error'].get('message'), value['error'].get('code')]
    elif isinstance(value.get('error'), str):
        messages.append(value['error'])
    for message in messages:
        if not isinstance(message, str):
            continue
        if message.strip() == 'Serverless with name is not found':
            return 'container_name_not_found'
        normalized = message.lower().replace('_', ' ')
        if any(marker in normalized for marker in ('quota exceeded', 'quota limit exceeded', 'exceeded quota')):
            return 'quota_exceeded'
        if any(marker in normalized for marker in ('insufficient capacity', 'not enough resources', 'insufficient resources')):
            return 'capacity_unavailable'
    return 'unclassified'


def read_retry(fn, *, attempts=4, label='Cloud read', statuses=(429, 500, 502, 503, 504)):
    """Bounded retries for reads only; callers must never pass a mutation."""
    for attempt in range(attempts):
        try:
            return fn()
        except CloudAPIError as error:
            if error.status not in statuses or error.reason in ('quota_exceeded', 'capacity_unavailable'):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if attempt == attempts - 1:
            raise ValueError(f'{label} unavailable after {attempts} attempts; preserve state and retry later') from None
        print(f'{label}: temporary failure; retry {attempt + 2}/{attempts}', file=sys.stderr, flush=True)
        time.sleep(min(2 ** attempt, 8) + random.uniform(0, .5))


class CloudAPI:
    def __init__(self, state, project_id):
        from setup import command_env
        self.project_id = str(uuid.UUID(project_id))
        env = command_env(Path(state))
        self.opener = urllib.request.build_opener(NoRedirect())
        result = self._request('https://iam.api.cloud.ru/api/v1/auth/token', 'POST',
                               {'keyId': env['AUTH_KEY_ID'], 'secret': env['AUTH_SECRET']}, {})
        self.token = result['access_token']

    def _request(self, url, method, data, headers):
        request = urllib.request.Request(url, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', **headers})
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            reason = error_reason(error.read(8192))
            raise CloudAPIError(error.code, method, reason) from None

    def request(self, service, path, *, method='GET', body=None, query=None):
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise ValueError('Use an API path and a separate query mapping')
        for values in (query, body):
            if isinstance(values, dict) and 'projectId' in values and values['projectId'] != self.project_id:
                raise ValueError('Cloud request belongs to another project')
        url = SERVICES[service] + path
        url += '?' + urllib.parse.urlencode({'projectId': self.project_id, **(query or {})})
        call = lambda: self._request(url, method, body, {'Authorization': 'Bearer ' + self.token})
        return read_retry(call, label=service + ' GET') if method == 'GET' else call()

    def organization(self, path, query=None):
        """Read-only quota metadata; never mutate organization limits or permissions."""
        if not re.fullmatch(r'/v3/(projects/[a-f0-9-]{36}|quotas/(resourceTypes|usages))', path):
            raise ValueError('Unsupported organization read')
        if path.startswith('/v3/projects/') and path.rsplit('/', 1)[-1] != self.project_id:
            raise ValueError('Quota lookup belongs to another project')
        url = 'https://organization.api.cloud.ru' + path
        if query:
            url += '?' + urllib.parse.urlencode(query, doseq=True)
        return read_retry(lambda: self._request(url, 'GET', None, {'Authorization': 'Bearer ' + self.token}), label='Quota GET')


def container_request(project_id, name, image, *, config, certificate, ca, key_secret):
    """No volumes, no database, no cloud API credentials inside the container."""
    uuid.UUID(project_id)
    if not re.fullmatch(r'[a-z][a-z0-9-]{3,48}[a-z0-9]', name):
        raise ValueError('Invalid container name')
    if not re.fullmatch(r'[a-z0-9-]+\.cr\.cloud\.ru/[a-z0-9/_-]+@sha256:[a-f0-9]{64}', image):
        raise ValueError('Use an immutable image digest in the owner Cloud.ru registry')
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]*', key_secret):
        raise ValueError('Container Apps currently requires a root-level Secret Management name')
    def variable(name, raw):
        return {'name': name, 'value': base64.b64encode(raw).decode()}
    startup = (Path(__file__).parent / 'edge/entrypoint.sh').read_text(encoding='utf-8')
    return {'projectId': project_id, 'name': name,
        'description': 'Portable Context HTTPS edge; memory remains on the owner VM',
        'configuration': {'ingress': {'publiclyAccessible': True, 'accessSettings': {'enableAuth': False}},
            'privileged': False, 'autoDeployments': {'enabled': False}, 'loggingService': {'isEnabled': False}},
        'template': {'timeout': '600s', 'idleTimeout': '60s', 'protocol': 'http_1',
            'scaling': {'minInstanceCount': 0, 'maxInstanceCount': 1,
                        'rule': {'type': 'concurrency', 'value': {'soft': 16, 'hard': 32}}},
            'containers': [{'name': 'pc-edge', 'image': image, 'containerPort': 8080,
                'resources': {'cpu': '0.1', 'memory': '256Mi'},
                'command': ['/bin/sh', '-ec'],
                'args': ['umask 077; printf \'%s\' "$PC_EDGE_STARTUP_B64" | base64 -d > /tmp/pc-edge-start; exec /bin/sh /tmp/pc-edge-start'],
                'env': [variable('PC_EDGE_CONFIG_B64', config), variable('PC_EDGE_CA_B64', ca),
                        variable('PC_EDGE_CERT_B64', certificate), variable('PC_EDGE_STARTUP_B64', startup.encode()),
                        {'name': 'PC_EDGE_KEY_B64', 'type': 'secret', 'value': key_secret}]}]},
        'metadata': {'createdByProduct': 'portable-context'}}


def public_origin(response):
    """Trust the actual provider-returned origin, never guess a name's URL."""
    uri = response['configuration']['ingress']['publicUri']
    if '://' not in uri:
        uri = 'https://' + hostname(uri)
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider did not return a plain HTTPS origin')
    if parsed.path not in ('', '/') or parsed.port not in (None, 443):
        raise ValueError('Unexpected public origin path or port')
    return 'https://' + hostname(parsed.hostname or '')
