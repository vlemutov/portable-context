"""Timeweb control plane. No redirects, secret-bearing errors or mutation retries."""
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cloud_gateway import NoRedirect
from setup import read


class TimewebError(RuntimeError):
    def __init__(self, status, method, error_code=None, response_id=None):
        self.status, self.method = status, method
        self.error_code=error_code if isinstance(error_code,str) and re.fullmatch(r'[a-z][a-z_]{0,79}',error_code) else None
        try:self.response_id=str(uuid.UUID(response_id)) if isinstance(response_id,str) else None
        except ValueError:self.response_id=None
        detail=' ('+self.error_code+')' if self.error_code else ''
        if self.response_id:detail+=' response_id='+self.response_id
        super().__init__(f'Timeweb HTTP {status}{detail} during {method}; response suppressed. Preserve state and resume.')


class API:
    def __init__(self, state):
        token = read(Path(state) / 'credentials.json').get('TIMEWEB_TOKEN', '')
        if not token or any(c.isspace() for c in token):
            raise ValueError('Run credentials in a local terminal for this Timeweb installation')
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, *, method='GET', body=None):
        if not re.fullmatch(r'/api/v[12]/[a-zA-Z0-9_./-]+', path) or '..' in path:
            raise ValueError('Invalid Timeweb API path')
        for attempt in range(4 if method == 'GET' else 1):
            req = urllib.request.Request('https://api.timeweb.cloud' + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                         'Accept': 'application/json', 'User-Agent': 'portable-context-setup'})
            try:
                with self.opener.open(req, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                code=None;response_id=None
                try:
                    value=json.loads(error.read(32768))
                    if isinstance(value,dict):code=value.get('error_code');response_id=value.get('response_id')
                except (ValueError,OSError,TypeError):pass
                error.close()
                if method != 'GET' or error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise TimewebError(error.code, method,code,response_id) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method != 'GET' or attempt == 3:
                    raise TimewebError(0, method) from None
            time.sleep(min(2 ** attempt, 8))


def origin(app):
    domains = [x['fqdn'] for x in app.get('domains', []) if x.get('is_technical')]
    if len(domains) != 1 or not re.fullmatch(r'[a-z0-9-]+\.twc1\.net', domains[0]):
        raise ValueError('Timeweb has not returned one supported technical HTTPS domain yet')
    return 'https://' + domains[0]


def integer(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('Expected a positive Timeweb resource ID')
    return int(value)
