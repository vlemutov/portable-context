"""Bounded system DNS/HTTPS readiness. Never changes hosts, routes or resolvers."""
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from cloud_gateway import NoRedirect
from setup import save


def sample(origin, expected_ip, path='/_pc/health', expected_body=None):
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.port not in (None,443) or parsed.query or parsed.fragment:
        raise ValueError('Expected plain HTTPS origin')
    expected_ip = str(ipaddress.ip_address(expected_ip))
    result = {'dns': False, 'https': False, 'reason': 'dns_pending'}
    try:
        # System getaddrinfo has no portable timeout. A short-lived local process
        # bounds it without leaving background threads or changing the resolver.
        script='import socket,json,sys; print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))'
        value=subprocess.run([sys.executable,'-c',script,parsed.hostname],capture_output=True,text=True,timeout=15)
        if value.returncode:return result
        answers=json.loads(value.stdout)
        result['addresses'] = answers
        result['dns'] = expected_ip in answers
        if not result['dns']:
            result['reason'] = 'dns_address_mismatch'
            return result
    except (OSError,subprocess.TimeoutExpired,ValueError):
        return result
    try:
        opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(urllib.request.Request(origin.rstrip('/')+path,headers={'Cache-Control':'no-cache'}),timeout=15) as response:
            body=response.read(4097)
            result['https']=response.status==200 and (expected_body is None or body==expected_body)
            result['reason']='ready' if result['https'] else 'https_identity_mismatch'
    except (OSError,urllib.error.URLError):
        result['reason']='https_pending'
    return result


def wait(state, origin, expected_ip, *, timeout=1800, interval=30, consecutive=3, probe=None, clock=time.monotonic, sleep=time.sleep):
    if not 0 < timeout <= 3600 or not 1 <= interval <= 60 or not 1 <= consecutive <= 10:
        raise ValueError('Invalid readiness bounds')
    deadline=clock()+timeout;streak=0
    while True:
        result=(probe or sample)(origin,expected_ip)
        streak=streak+1 if result.get('dns') and result.get('https') else 0
        receipt={'origin':origin,'expected_ip':expected_ip,'observed_at':time.time(),
                 'consecutive':streak,'required':consecutive,'result':result,'ready':streak>=consecutive}
        save(state/'dns-readiness.json',receipt)
        if receipt['ready']:return receipt
        if clock()>=deadline:
            raise ValueError('DNS/HTTPS readiness timed out. Resources retained. Resume install --https with the SAME --state; do not create another app or edit hosts/routes.')
        print('Waiting for DNS/HTTPS: '+result.get('reason','pending')+'; same application retained',file=sys.stderr,flush=True)
        sleep(min(interval,max(0,deadline-clock())))
