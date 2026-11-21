"""Read-only DNS evidence from a client or temporary cloud fixture.

Only fixed public DNS services and Timeweb/.net authorities are contacted.
The target must be one technical hostname; no arbitrary URL or command input.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import ipaddress
import json
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request

import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.resolver

AUTHORITIES = ('ns1.timeweb.ru', 'ns2.timeweb.ru', 'ns3.timeweb.org', 'ns4.timeweb.org')


def doh(name, kind='A', provider='google', ecs=None):
    base = 'https://dns.google/resolve' if provider == 'google' else 'https://cloudflare-dns.com/dns-query'
    params = {'name': name, 'type': kind}
    if ecs is not None:
        params['edns_client_subnet'] = ecs
    # Use the native TLS stack on Windows; no shell, no cloud credential headers.
    response = subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '8',
        '-H', 'Accept: application/dns-json', base + '?' + urllib.parse.urlencode(params)],
        capture_output=True, timeout=12, check=True)
    return json.loads(response.stdout[:131072])


def wire(name, kind, address, tcp):
    if not ipaddress.ip_address(address).is_global:
        raise ValueError('Authority must have a public address')
    query = dns.message.make_query(name, kind)
    query.flags &= ~dns.flags.RD
    reply = (dns.query.tcp if tcp else dns.query.udp)(query, address, timeout=4)
    return {'rcode': dns.rcode.to_text(reply.rcode()), 'flags': dns.flags.to_text(reply.flags),
        'answer': [r.to_text() for r in reply.answer],
        'authority': [r.to_text() for r in reply.authority],
        'additional': [r.to_text() for r in reply.additional]}


def measured(label, action):
    start = time.monotonic()
    try:
        result = {'result': action()}
    except Exception as error:
        result = {'error': type(error).__name__}
    return label, {**result, 'seconds': round(time.monotonic() - start, 3)}


def collect(hostname, cloud_ip=None, system_lookup=None):
    if not re.fullmatch(r'[a-z0-9-]{1,63}\.twc1\.net', hostname):
        raise ValueError('Expected one Timeweb technical hostname')
    if cloud_ip and not ipaddress.ip_address(cloud_ip).is_global:
        raise ValueError('Expected public test app IP')
    evidence = {'observed_at': datetime.now(timezone.utc).isoformat(), 'hostname': hostname,
        'queries': {}, 'authority_addresses': {}}
    tasks = {
        'system_A': lambda: sorted({v[4][0] for v in (system_lookup or socket.getaddrinfo)(hostname, 443, socket.AF_INET)}),
        'google_A_default': lambda: doh(hostname),
        'google_A_no_ecs': lambda: doh(hostname, ecs='0.0.0.0/0'),
        'google_AAAA_no_ecs': lambda: doh(hostname, kind='AAAA', ecs='0.0.0.0/0'),
        'cloudflare_A': lambda: doh(hostname, provider='cloudflare'),
        'google_zone_NS': lambda: doh('twc1.net', kind='NS', ecs='0.0.0.0/0'),
        'cloudflare_zone_NS': lambda: doh('twc1.net', kind='NS', provider='cloudflare'),
    }
    if cloud_ip:
        tasks['google_A_cloud_ecs'] = lambda: doh(hostname, ecs=cloud_ip + '/32')
    for authority in (*AUTHORITIES, 'a.gtld-servers.net'):
        tasks['address_' + authority] = lambda n=authority: doh(n, provider='cloudflare')
    with ThreadPoolExecutor(max_workers=10) as pool:
        for future in as_completed([pool.submit(measured, name, action) for name, action in tasks.items()]):
            name, result = future.result()
            evidence['queries'][name] = result
    tasks = {}
    for authority in (*AUTHORITIES, 'a.gtld-servers.net'):
        lookup = evidence['queries'].get('address_' + authority, {}).get('result', {})
        addresses = [r['data'] for r in lookup.get('Answer', []) if r.get('type') == 1]
        evidence['authority_addresses'][authority] = addresses
        for address in addresses[:2]:
            for tcp in (False, True):
                kinds = [('twc1.net', 'NS')] if authority == 'a.gtld-servers.net' else [(hostname, 'A'), ('twc1.net', 'SOA')]
                for target, kind in kinds:
                    label = f'{authority}_{address}_{"tcp" if tcp else "udp"}_{kind}'
                    tasks[label] = lambda n=target, k=kind, a=address, t=tcp: wire(n, k, a, t)
    with ThreadPoolExecutor(max_workers=12) as pool:
        for future in as_completed([pool.submit(measured, name, action) for name, action in tasks.items()]):
            name, result = future.result()
            evidence['queries'][name] = result
    return evidence
