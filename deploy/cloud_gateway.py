"""Cloud.ru edge API adapter. Uses owner credentials from private deployment state.

All mutations are explicit. Errors deliberately omit response bodies: providers
can echo submitted environment values. No automatic retry of a mutation.
"""
from __future__ import annotations
import base64
import json
from pathlib import Path
import re
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
            raise RuntimeError(f'Cloud API HTTP {error.code}; operation {method}; response body suppressed') from None

    def request(self, service, path, *, method='GET', body=None, query=None):
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise ValueError('Use an API path and a separate query mapping')
        url = SERVICES[service] + path
        url += '?' + urllib.parse.urlencode({'projectId': self.project_id, **(query or {})})
        return self._request(url, method, body, {'Authorization': 'Bearer ' + self.token})


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
