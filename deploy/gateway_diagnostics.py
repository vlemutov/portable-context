"""Opt-in, bounded GET probes. Never records bodies, credentials or exception text.

Public probes distinguish edge health, mTLS transport and MCP metadata/challenge.
VM probes independently check the origin over mTLS and the loopback MCP process.
No mutation, redirect, cloud publication, authentication or retry is performed.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

PATHS = {'edge': '/_pc/health', 'transport': '/_pc/transport',
         'metadata': '/.well-known/oauth-protected-resource/mcp', 'challenge': '/mcp'}
REVISION = re.compile(r'(?:initial|origin-test|edge-test|g-[a-f0-9]{32}-[a-z_]+)\Z')
REQUEST_ID = re.compile(r'(?:[a-fA-F0-9]{16,64}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\Z')


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_headers(headers):
    """Positive allowlist; arbitrary headers and unmatched values are discarded."""
    result = {}
    for key, value in headers:
        key = key.lower()
        if key in ('x-pc-edge-revision', 'x-pc-origin-revision') and REVISION.fullmatch(value):
            result[key] = value
        elif key == 'x-pc-failure-layer' and value in ('edge', 'origin'):
            result[key] = value
        elif key in ('x-request-id', 'x-correlation-id', 'x-cloud-trace-id') and REQUEST_ID.fullmatch(value):
            result[key] = value
        elif key == 'server' and value.lower() in ('envoy', 'caddy', 'nginx'):
            result[key] = value.lower()
    return result


def error_category(error):
    if isinstance(error, ssl.SSLCertVerificationError): return 'tls_verification'
    if isinstance(error, ssl.SSLError): return 'tls'
    if isinstance(error, (TimeoutError, socket.timeout)): return 'timeout'
    if isinstance(error, ConnectionRefusedError): return 'connection_refused'
    if isinstance(error, ConnectionError): return 'connection'
    if isinstance(error, http.client.HTTPException): return 'http_protocol'
    if isinstance(error, OSError): return 'network'
    return 'probe_setup'


def probe(address, port, host, kind, *, context=None, timeout=12):
    """Exactly one fixed-path GET. The caller cannot supply headers or a body."""
    path = PATHS[kind]
    record = {'started_at': now(), 'kind': kind, 'path': path}
    start = time.monotonic()
    try:
        with closing(http.client.HTTPConnection(address, port, timeout=timeout)) as conn:
            if context is not None:
                raw = socket.create_connection((address, port), timeout=timeout)
                try: conn.sock = context.wrap_socket(raw, server_hostname=host)
                except Exception:
                    raw.close()
                    raise
                record['peer_certificate_sha256'] = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            conn.request('GET', path, headers={'Host': host, 'Connection': 'close',
                'Cache-Control': 'no-cache', 'Accept': 'application/json, text/event-stream'})
            response = conn.getresponse()
            record['status'] = response.status
            record['headers'] = safe_headers(response.getheaders())
            # Fixed probe endpoints only. Never serialize or hash a response body.
            body = response.read(65537)
            valid = False
            if len(body) <= 65536:
                if kind == 'metadata' and response.status == 200:
                    try:
                        value = json.loads(body)
                        valid = (isinstance(value, dict) and value.get('resource') == 'https://'+host+'/mcp'
                                 and value.get('authorization_servers') == ['https://'+host])
                    except (ValueError, UnicodeError): pass
                elif kind == 'challenge':
                    valid = response.status == 401 and 'resource_metadata' in response.getheader('WWW-Authenticate', '')
                elif kind == 'edge':
                    valid = response.status == 200 and body == b'edge alive; not a memory readiness check'
                elif kind == 'transport':
                    try:
                        revision = body.decode('ascii')
                        valid = response.status == 200 and bool(REVISION.fullmatch(revision))
                        if valid: record['origin_revision'] = revision
                    except UnicodeError: pass
            record['ok'] = valid
    except Exception as error:
        record.update(ok=False, error=error_category(error))
    record.update(finished_at=now(), elapsed_ms=round((time.monotonic()-start)*1000, 2))
    return record


class Trace:
    """One fresh private JSONL, capped at 16 MiB; never overwrite old evidence."""
    def __init__(self, path, max_bytes=16*1024*1024):
        path = Path(path);path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w', encoding='utf-8', newline='\n')
        self.lock = threading.Lock();self.max_bytes = max_bytes;self.size = 0
        self.counts = {};self.dropped = 0;self.write_failed = False

    def append(self, record):
        line = json.dumps(record, separators=(',', ':'))+'\n'
        with self.lock:
            count = self.counts.setdefault(record['target']+':'+record['kind'], {'requests': 0, 'failures': 0})
            count['requests'] += 1;count['failures'] += int(not record['ok'])
            if self.size+len(line.encode()) > self.max_bytes or self.write_failed:
                self.dropped += 1;return
            try:
                self.file.write(line);self.file.flush();self.size += len(line.encode())
            except OSError:
                self.write_failed = True;self.dropped += 1

    def close(self):
        self.file.close()
        return {'probes': self.counts, 'dropped': self.dropped, 'write_failed': self.write_failed}


def vm_context(state):
    from gateway_rotation import read, rotation_revision
    state = Path(state);active = read(state/'active.json')['generation']
    journal = read(state/'rotation.json') if (state/'rotation.json').exists() else None
    names = [active];client = active
    if journal and not journal.get('complete'):
        names = [journal['old'], journal['new']]
        if journal['stage'] != 'origin_overlap': client = journal['new']
    roots = state/'generations'
    names = [rotation_revision(name) for name in names]
    # During atomic preparation the new generation may not yet exist.
    ca = ''.join((roots/name/'origin-ca.pem').read_text() for name in names if (roots/name/'origin-ca.pem').exists())
    ctx = ssl.create_default_context(cadata=ca)
    ctx.load_cert_chain(roots/client/'edge.pem', roots/client/'edge-key.pem')
    return ctx


def monitor(public_origin, output, seconds, interval=1, state=None, stop=None):
    url = urlsplit(public_origin)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443) or url.path not in ('', '/') or url.query or url.fragment:
        raise ValueError('Use the public HTTPS origin without credentials or path')
    if not 1 <= seconds <= 7200 or not .5 <= interval <= 60: raise ValueError('Bounded probe duration/interval required')
    from gateway import hostname
    host = hostname(url.hostname);stop = stop or threading.Event()
    deadline = time.monotonic()+seconds
    config = None
    if state:
        from gateway_rotation import read
        config = read(Path(state)/'config.json')
        if config['public_host'] != host: raise ValueError('Controller belongs to another host')
    trace = Trace(output)
    targets = [('public', k) for k in PATHS] if not state else [('vm-origin', 'transport'), ('vm-mcp', 'metadata')]
    def worker(target, kind):
        context = ssl.create_default_context()
        while time.monotonic() < deadline and not stop.is_set():
            try:
                if target == 'public': result = probe(host, 443, host, kind, context=context)
                elif target == 'vm-origin': result = probe('127.0.0.1', config.get('origin_port', 9443), host, kind, context=vm_context(state))
                else: result = probe('127.0.0.1', config.get('application_port', 8767), host, kind)
            except Exception as error:
                result = {'started_at': now(), 'finished_at': now(), 'kind': kind, 'ok': False, 'error': error_category(error)}
            result['target'] = target;trace.append(result);stop.wait(interval)
    threads = [threading.Thread(target=worker,args=item) for item in targets]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join()
    finally:
        stop.set()
        for thread in threads:
            if thread.is_alive(): thread.join(timeout=15)
    return trace.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--public-origin', required=True);p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=1800);p.add_argument('--interval', type=float, default=1)
    p.add_argument('--state', type=Path)
    a = p.parse_args();print(json.dumps(monitor(a.public_origin, a.output, a.seconds, a.interval, a.state)))


if __name__ == '__main__': main()
