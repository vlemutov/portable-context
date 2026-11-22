"""Portable IPv4 route helper. Inspect -> plan -> apply; remove only recorded own routes.

No VPN configuration, default route, firewall rule, or cloud resource is changed here.
State belongs beside deployment.json, outside the repository. Python standard library only.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import ssl
import subprocess
import struct
import tempfile
import time

CLOUD_HOSTS = ('console.cloud.ru', 'iam.api.cloud.ru', 'compute.api.cloud.ru',
               'vpc.api.cloud.ru', 'organization.api.cloud.ru', 's3.cloud.ru',
               'ar.api.cloud.ru','containers.api.cloud.ru','secretmanager.api.cloud.ru')
METRIC = 17383


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n')
            if os.name != 'nt' and os.geteuid() == 0:
                owner = path.parent.stat()
                os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local or int(address) == 0xffffffff:
        raise ValueError('Use a unicast IPv4 address, not loopback/link-local/multicast')
    return str(address)


def route_record(destination, gateway, interface):
    interface = str(interface)
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}', interface):
        raise ValueError('Invalid interface identifier')
    return {'destination': ipv4(destination), 'gateway': ipv4(gateway), 'interface': interface}


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                            env=dict(os.environ, LC_ALL='C', LANG='C'), timeout=30)
    if result.returncode:
        raise RuntimeError('Network command failed: ' + (result.stderr.strip() or result.stdout.strip())[:1200])
    return result.stdout


def powershell(script):
    encoded = base64.b64encode(('[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); '
                               "$ErrorActionPreference='Stop'; " + script).encode('utf-16-le')).decode()
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]


def parse_routes(system, text):
    """Normalize active routing tables, keeping host routes and default-gateway candidates."""
    routes, candidates = [], []
    if system == 'Windows':
        rows = json.loads(text or '[]')
        if isinstance(rows, dict):
            rows = [rows]
        rows = [(r['DestinationPrefix'], r['NextHop'], str(r['InterfaceIndex']),
                 bool(r.get('Physical', False))) for r in rows]
    elif system == 'Linux':
        rows = [(r.get('dst', ''), r.get('gateway', '0.0.0.0'), r.get('dev', ''),
                 not re.match(r'^(tun|tap|wg|tailscale|docker|br-|virbr|lo)', r.get('dev', '')))
                for r in json.loads(text)]
    elif system == 'Darwin':
        rows = []
        index = None
        for line in text.splitlines():
            fields = line.split()
            if fields and fields[0] == 'Destination' and 'Netif' in fields:
                index = fields.index('Netif')
            elif index is not None and len(fields) > index:
                dev = fields[index]
                rows.append((fields[0], fields[1], dev, dev.startswith('en')))
    else:
        raise ValueError('Supported clients: Windows, macOS and Linux')
    for destination, gateway, interface, physical in rows:
        try:
            ipv4(gateway)
            if destination in ('default', '0.0.0.0/0'):
                if physical:
                    candidate = {'interface': interface, 'gateway': gateway}
                    if candidate not in candidates:
                        candidates.append(candidate)
            elif '/' not in destination or destination.endswith('/32'):
                routes.append(route_record(destination.removesuffix('/32'), gateway, interface))
        except (ValueError, TypeError):
            continue
    return {'candidates': candidates, 'routes': routes}


def snapshot(system):
    if system == 'Windows':
        command = powershell("$physical=@(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | ForEach-Object ifIndex); "
            "@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | ForEach-Object { "
            "[pscustomobject]@{DestinationPrefix=$_.DestinationPrefix;NextHop=$_.NextHop;"
            "InterfaceIndex=$_.InterfaceIndex;Physical=($physical -contains $_.InterfaceIndex)} }) | ConvertTo-Json -Compress")
    elif system == 'Linux':
        command = ['ip', '-j', '-4', 'route', 'show', 'table', 'main']
    elif system == 'Darwin':
        command = ['/usr/sbin/netstat', '-rn', '-f', 'inet']
    else:
        raise ValueError('Supported clients: Windows, macOS and Linux')
    return parse_routes(system, run(command))


def command(system, action, record):
    r = route_record(**record)
    ip, gateway, dev = r['destination'], r['gateway'], r['interface']
    if action not in ('add', 'delete'):
        raise ValueError('Invalid route action')
    if system == 'Windows':
        if not dev.isdigit():
            raise ValueError('Windows requires a numeric interface index')
        verb = 'New-NetRoute' if action == 'add' else 'Remove-NetRoute'
        options = f" -DestinationPrefix '{ip}/32' -InterfaceIndex {int(dev)} -NextHop '{gateway}' -PolicyStore ActiveStore"
        if action == 'add':
            return powershell(verb + options + f' -RouteMetric {METRIC} | Out-Null')
        return powershell('Get-NetRoute' + options + f' | Where-Object RouteMetric -eq {METRIC}'
                          + ' | Remove-NetRoute -Confirm:$false | Out-Null')
    if system == 'Linux':
        # Never use replace/flush: unrelated routes are not ours to overwrite.
        args = ['ip', '-4', 'route', 'add' if action == 'add' else 'del', ip + '/32',
                'via', gateway, 'dev', dev, 'table', 'main', 'metric', str(METRIC)]
        return args
    if system == 'Darwin':
        # Unscoped host route takes precedence over a full-tunnel VPN's default routes.
        # The gateway is selected from the physical interface's default route.
        return ['/sbin/route', '-n', action, '-host', ip, gateway]
    raise ValueError('Unsupported operating system')


def registry_hosts(state):
    """Derive the data-plane hostname from this deployment's receipts, not user guesses."""
    hosts=set()
    path=state/'https-journal.json'
    if path.exists():
        value=read(path);config=read(state/'deployment.json')
        if value.get('config',{}).get('project_id')!=config.get('project_id'):
            raise ValueError('HTTPS journal belongs to another project')
        name=value.get('completed',{}).get('registry',{}).get('name','')
        if name:
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}',name):raise ValueError('Invalid recorded registry name')
            hosts.add(name+'.cr.cloud.ru')
    return sorted(hosts)


def targets(config, scope, state=None, tolerate_dns=False):
    endpoints = []
    if scope in ('cloud', 'all'):
        if config['mode'] != 'cloudru':
            raise ValueError('Cloud endpoints apply only to a cloudru deployment')
        endpoints.extend((host, 443) for host in CLOUD_HOSTS)
        if state:endpoints.extend((host,443) for host in registry_hosts(state))
    if scope in ('server', 'all'):
        if not config.get('host'):
            raise ValueError('Server not created yet; use --scope cloud first')
        endpoints.append((config['host'], int(config.get('ssh_port', 22))))
    resolved = []
    for host, port in endpoints:
        try:addresses = sorted({ipv4(row[4][0]) for row in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
        except OSError:
            if not tolerate_dns:raise ValueError('DNS lookup failed for '+host) from None
            resolved.append({'host':host,'port':port,'addresses':[],'dns':'failed'});continue
        if not addresses:
            raise ValueError('No IPv4 addresses for ' + host)
        resolved.append({'host': host, 'port': port, 'addresses': addresses})
    return resolved


def probe(endpoint):
    results = []
    for ip in endpoint['addresses']:
        try:
            with socket.create_connection((ip, endpoint['port']), timeout=3) as connection:
                result={'address':ip,'tcp':'reachable'}
                results.append(result)
                if endpoint['port']==443:
                    try:
                        with ssl.create_default_context().wrap_socket(connection,server_hostname=endpoint['host']) as tls:
                            result['tls']='verified'
                            tls.sendall(('HEAD / HTTP/1.1\r\nHost: '+endpoint['host']+'\r\nConnection: close\r\n\r\n').encode('ascii'))
                            line=tls.recv(512).split(b'\r\n',1)[0].split()
                            if len(line)>=2 and line[0].startswith(b'HTTP/') and line[1].isdigit():result['http_status']=int(line[1])
                    except (OSError,ValueError):result['tls_or_http']='failed'
        except OSError as exc:
            results.append({'address': ip, 'tcp': 'failed', 'reason': type(exc).__name__})
    return dict(endpoint, results=results)


def inspect(state, scope):
    system = platform.system()
    network = snapshot(system)
    endpoints = targets(read(state / 'deployment.json'), scope,state,tolerate_dns=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        checks = list(pool.map(probe, endpoints))
    policy={}
    if system=='Linux':
        for key,args in [('rules',['ip','-j','-4','rule','show']),('all_tables',['ip','-j','-4','route','show','table','all'])]:
            try:policy[key]=json.loads(run(args))
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):policy[key]='unavailable'
        policy['effective_routes']=[]
        for ip, port in sorted({(ip, e['port']) for e in endpoints for ip in e['addresses']}):
            try:
                route = tcp_route(ip, port, state.stat().st_uid)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                route = 'unavailable'
            policy['effective_routes'].append({'address': ip, 'port': port, 'route': route})
    ledger=read(state/'network-routes.json') if (state/'network-routes.json').exists() else {}
    return {'os': system, **network, 'endpoints': checks,'linux_policy':policy,
            'missing_owned_routes':[r for r in ledger.get('owned',[]) if r not in network['routes']],
            'note': 'IPv4 current path only; TCP/TLS/HTTP do not prove country or authorization. Cloud HTTPS and VM SSH may need DIFFERENT paths. Linux route-get does not model every VPN fwmark/kill switch. No VPN/default route changes.'}


def scope_members(scope, endpoints):
    # targets() appends the VM last for 'all'; a shared endpoint can belong to both scopes.
    groups = {}
    if scope in ('all', 'server'):
        groups['server'] = list(endpoints[-1]['addresses'])
    if scope in ('all', 'cloud'):
        cloud = endpoints[:-1] if scope == 'all' else endpoints
        groups['cloud'] = sorted({ip for endpoint in cloud for ip in endpoint['addresses']})
    return groups


def scoped_changes(ledger, groups, desired, *, all_scopes=False):
    previous = ledger.get('scopes', {})
    updated = dict(previous, **groups)
    retained = {ip for ips in updated.values() for ip in ips}
    touched = {ip for scope in groups for ip in previous.get(scope, [])}
    wanted = {r['destination']: r for r in desired}
    remove = []
    for old in ledger.get('owned', []):
        ip = old['destination']
        if ip in wanted and old != wanted[ip]:
            if any(ip in ips for scope, ips in previous.items() if scope not in groups):
                raise ValueError('Shared host route needs one gateway; review a plan for all scopes')
            remove.append(old)
        elif ip not in retained and (all_scopes or ip in touched):
            remove.append(old)
    return updated, remove


def make_plan(state, scope, interface=None, gateway=None, ssh_policy=False):
    system = platform.system()
    config = read(state / 'deployment.json')
    network = snapshot(system)
    candidates = [c for c in network['candidates']
                  if (interface is None or c['interface'] == str(interface))
                  and (gateway is None or c['gateway'] == gateway)]
    if len(candidates) != 1:
        raise ValueError('Select one physical gateway with --interface and, if needed, --gateway. Candidates: '
                         + json.dumps(network['candidates']))
    selected = candidates[0]
    if system == 'Darwin' and len([c for c in network['candidates'] if c['gateway'] == selected['gateway']]) > 1:
        raise ValueError('Multiple macOS interfaces share this gateway; configure VPN split routing explicitly')
    endpoints = targets(config, scope,state)
    desired = [route_record(ip, **selected) for ip in sorted({ip for e in endpoints for ip in e['addresses']})]
    ledger = read(state / 'network-routes.json') if (state / 'network-routes.json').exists() else None
    if ssh_policy and (system != 'Linux' or scope != 'server'):
        raise ValueError('--ssh-policy requires Linux and --scope server')
    groups = scope_members(scope, endpoints)
    _, remove = scoped_changes(ledger or {}, groups, desired, all_scopes=scope == 'all')
    plan = {'format': 2, 'os': system, 'created': time.time(), 'configuration': digest(config),
            'ownership': digest(ledger), 'remove_owned': remove, 'ssh_policy': ssh_policy,
            'scope': scope, 'endpoints': endpoints, 'desired': desired}
    if ssh_policy:
        plan['conditional_rule'] = 'Only if TCP route still misses this gateway: to VM/32 lookup main, before VPN rules'
    save(state / 'network-plan.json', plan)
    return {'plan_sha256': digest(plan), **plan,
            'next': 'Review these host routes, then apply --approve-plan HASH with administrator/root rights. No default route or VPN settings are changed.'}


@contextmanager
def locked(state):
    path = state / 'network.lock'
    try:
        stream = path.open('x')
    except FileExistsError:
        raise ValueError('Another route operation is active, or a previous process crashed. Inspect network.lock before retrying.') from None
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


def require_admin():
    if os.name == 'nt':
        import ctypes
        elevated = ctypes.windll.shell32.IsUserAnAdmin()
    else:
        elevated = os.geteuid() == 0
    if not elevated:
        raise PermissionError('Route changes require an administrator terminal on Windows or sudo on macOS/Linux. Inspect and plan do not need elevation.')


def tcp_route(destination, port, uid, interface=None):
    args = ['ip', '-j', '-4', 'route', 'get', ipv4(destination), 'ipproto', 'tcp',
            'dport', str(int(port)), 'uid', str(uid)]
    if interface:
        args.extend(['oif', interface])
    rows = json.loads(run(args))
    if len(rows) != 1:
        raise ValueError('Cannot identify a single effective IPv4 path')
    return rows[0]


def physical_path(actual, record):
    return actual.get('dev') == record['interface'] and actual.get('gateway') == record['gateway']


def linux_rules():
    return json.loads(run(['ip', '-j', '-4', 'rule', 'show']))


def rule_present(rule, rows):
    same = [r for r in rows if r['priority'] == rule['priority']]
    if not same:
        return False
    r = same[0]
    if (len(same) != 1 or r.get('dst', '').removesuffix('/32') != rule['destination']
            or r.get('src') != 'all' or r.get('table') not in ('main', 254)
            or set(r) - {'priority', 'src', 'dst', 'table', 'protocol', 'flags'}
            or r.get('flags') or r.get('protocol', 'boot') not in ('boot', 3)):
        raise ValueError('Policy priority already used or externally changed; inspect ip -4 rule')
    return True


def rule_command(action, rule):
    return ['ip', '-4', 'rule', action, 'priority', str(int(rule['priority'])),
            'to', ipv4(rule['destination']) + '/32', 'lookup', 'main', 'protocol', 'boot']


def ensure_ssh_path(state, record, port):
    path = state / 'network-routes.json'
    ledger = read(path)
    ip = record['destination']
    uid = state.stat().st_uid
    rules = ledger.setdefault('ssh_rules', {})
    rows = linux_rules()
    present = ip in rules and rule_present(rules[ip], rows)
    if physical_path(tcp_route(ip, port, uid), record):
        return
    if present:
        raise ValueError('Owned rule is present but SSH still misses the physical gateway; inspect VPN policy')
    # Keep the kernel local rule first. Never edit a VPN rule or a VPN-owned table.
    if any(r['priority'] == 0 and r.get('table') not in ('local', 255) for r in rows):
        raise ValueError('Higher-priority policy at priority zero; inspect VPN rules')
    first = min((r['priority'] for r in rows if r['priority'] > 0), default=32766)
    if first <= 1:
        raise ValueError('No free priority before routing policy; inspect VPN rules')
    rule = {'destination': ip, 'priority': first - 1}
    ledger['pending'] = {'add_rule': rule}
    save(path, ledger)
    try:
        run(rule_command('add', rule))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if not rule_present(rule, linux_rules()):
            del ledger['pending']
            save(path, ledger)
        raise
    rules[ip] = rule
    del ledger['pending']
    save(path, ledger)
    if not physical_path(tcp_route(ip, port, uid), record):
        raise ValueError('SSH still misses the selected gateway. Inspect policy or remove --scope server')


def measure_egress(state):
    """Opt-in: direct TLS socket pinned to the physical device and local address."""
    system = platform.system()
    require_admin()
    config = read(state / 'deployment.json')
    endpoint, = targets(config, 'server', state)
    network = snapshot(system)
    if len(endpoint['addresses']) != 1:
        raise ValueError('SSH egress measurement requires one VM IPv4')
    vm = endpoint['addresses'][0]
    if system == 'Linux':
        actual = tcp_route(vm, endpoint['port'], state.stat().st_uid)
        matches = [c for c in network['candidates'] if physical_path(actual, c)]
    elif system == 'Windows':
        script = (f"$r=Find-NetRoute -RemoteIPAddress '{ipv4(vm)}'; "
                  "$r | Where-Object DestinationPrefix | Select-Object InterfaceIndex,NextHop | ConvertTo-Json -Compress")
        route = json.loads(run(powershell(script)))
        matches = [c for c in network['candidates'] if c == {
            'interface': str(route['InterfaceIndex']), 'gateway': route['NextHop']}]
    elif system == 'Darwin':
        route = run(['/sbin/route', '-n', 'get', vm])
        fields = dict(re.findall(r'^\s*(interface|gateway):\s*(\S+)', route, re.M))
        matches = [c for c in network['candidates'] if c == fields]
    else:
        raise ValueError('Unsupported operating system')
    if len(matches) != 1:
        raise ValueError('SSH path is not verified through one physical gateway')
    physical = matches[0]
    host = 'api.ipify.org'
    peer = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    if system == 'Linux':
        path = tcp_route(peer, 443, os.geteuid(), physical['interface'])
        if not actual.get('prefsrc'):
            raise ValueError('Kernel did not identify the local IPv4 used for SSH')
        local = ipv4(actual['prefsrc'])
        if not physical_path(path, physical) or path.get('prefsrc') != local:
            raise ValueError('Cannot verify the IP reflector uses the same physical gateway and source as SSH')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(6)
        if system == 'Linux':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, physical['interface'].encode() + b'\0')
            sock.bind((local, 0))
        elif system == 'Windows':
            # Winsock IP_UNICAST_IF uses a network-byte-order interface index.
            sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack('!I', int(physical['interface'])))
        else:
            sock.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(physical['interface']))  # Darwin IP_BOUND_IF
        sock.connect((peer, 443))
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall(b'GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n')
            with http.client.HTTPResponse(tls) as response:
                response.begin()
                if response.status != 200:
                    raise ValueError('Public IPv4 service returned HTTP ' + str(response.status))
                address = ipaddress.IPv4Address(response.read(64).decode('ascii').strip())
    if not address.is_global or address.is_multicast:
        raise ValueError('The measured IPv4 is not public unicast')
    return {**physical, 'vm': vm, 'current_ssh_cidr': config.get('ssh_cidr'),
            'suggested_ssh_cidr': str(address) + '/32', 'source': host}


def reconcile(state, desired, *, removing=False, groups=None, all_scopes=False):
    """Persist each successful mutation. Never adopt or delete a pre-existing route."""
    system = platform.system()
    ledger_path = state / 'network-routes.json'
    ledger = read(ledger_path) if ledger_path.exists() else {'format': 1, 'os': system, 'owned': []}
    if ledger['os'] != system:
        raise ValueError('Route ownership belongs to another operating system')
    if ledger.get('pending'):
        raise ValueError('An interrupted route change requires inspection of network-routes.json; ownership will not be guessed')
    for r in ledger['owned'] + desired:
        if route_record(**r) != r:
            raise ValueError('Invalid route record')
    current = snapshot(system)['routes']
    # Preflight all conflicts before changing anything; changing a previously owned gateway is allowed.
    for wanted in desired:
        collisions = [r for r in current if r['destination'] == wanted['destination']]
        if any(r != wanted and r not in ledger['owned'] for r in collisions):
            raise ValueError('An unrelated host route conflicts with ' + wanted['destination'])
    if groups is None:
        remove = [r for r in ledger['owned'] if r not in desired]
    else:
        updated, remove = scoped_changes(ledger, groups, desired, all_scopes=all_scopes)
        # Retain both old and new memberships until all mutations have succeeded.
        interim = ledger.setdefault('scopes', {})
        for scope, ips in groups.items():
            interim[scope] = sorted(set(interim.get(scope, [])) | set(ips))
        save(ledger_path, ledger)
    for ip, rule in list(ledger.get('ssh_rules', {}).items()):
        if groups is not None and 'server' not in groups:
            continue
        if groups is not None and ip in groups.get('server', []):
            continue
        if groups is None and not removing:
            continue
        if rule_present(rule, linux_rules()):
            run(rule_command('del', rule))
        del ledger['ssh_rules'][ip]
        save(ledger_path, ledger)
    for old in remove:
        if old in desired:
            continue
        observed = snapshot(system)['routes']
        if old in observed:
            run(command(system, 'delete', old))
        elif any(r['destination'] == old['destination'] for r in observed):
            raise ValueError('Managed route changed externally; refusing to remove ' + old['destination'])
        ledger['owned'].remove(old)
        save(ledger_path, ledger)
    reused = []
    for wanted in desired:
        observed = snapshot(system)['routes']
        if wanted in observed:
            if wanted not in ledger['owned']:
                reused.append(wanted)
            continue
        if any(r['destination'] == wanted['destination'] for r in observed):
            raise ValueError('Routing table changed during apply; retry diagnosis')
        # Save intent so interrupted operations remain diagnosable; never infer ownership from intent alone.
        ledger['pending'] = wanted
        save(ledger_path, ledger)
        try:
            run(command(system, 'add', wanted))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            if wanted not in snapshot(system)['routes']:
                ledger.pop('pending', None)
                save(ledger_path, ledger)
            raise
        if wanted not in ledger['owned']:
            ledger['owned'].append(wanted)
        ledger.pop('pending', None)
        save(ledger_path, ledger)
    if groups is not None:
        ledger['scopes'] = updated
    if removing or groups is not None:
        save(ledger_path, ledger)
    return {'owned': ledger['owned'], 'preexisting_reused': reused,
            'next': 'Run inspect and setup.py check. Routes are session-scoped; after reboot/network change generate and apply a fresh plan.'}


def apply(state, approval):
    with locked(state):
        plan = read(state / 'network-plan.json')
        if plan.get('format') != 2:
            raise ValueError('Regenerate the scoped network plan with this installer version')
        if digest(plan) != approval or plan['configuration'] != digest(read(state / 'deployment.json')):
            raise ValueError('Plan or deployment changed; regenerate and review the plan')
        ledger = read(state / 'network-routes.json') if (state / 'network-routes.json').exists() else None
        if plan['ownership'] != digest(ledger):
            raise ValueError('Route ownership changed; regenerate the plan')
        if plan['os'] != platform.system() or not 0 <= time.time() - plan['created'] <= 600:
            raise ValueError('Plan expired or belongs to another OS; regenerate it')
        candidates = snapshot(platform.system())['candidates']
        if any({'interface': r['interface'], 'gateway': r['gateway']} not in candidates for r in plan['desired']):
            raise ValueError('Network changed; regenerate the plan with the current gateway')
        expected = targets(read(state / 'deployment.json'), plan['scope'],state)
        if expected != plan['endpoints']:
            raise ValueError('DNS changed; regenerate the plan')
        if {r['destination'] for r in plan['desired']} != {ip for e in expected for ip in e['addresses']}:
            raise ValueError('Plan destinations do not match the deployment endpoints')
        require_admin()
        groups = scope_members(plan['scope'], expected)
        result = reconcile(state, plan['desired'], groups=groups, all_scopes=plan['scope'] == 'all')
        if plan['ssh_policy']:
            if platform.system() != 'Linux' or plan['scope'] != 'server':
                raise ValueError('Invalid scope for Linux SSH policy')
            for route in plan['desired']:
                ensure_ssh_path(state, route, expected[0]['port'])
            result['ssh_route'] = 'verified for installation owner; check authentication separately'
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'plan', 'repair'):
        p = sub.add_parser(name, help='repair generates a fresh plan; it never guesses elevated approval')
        p.add_argument('--scope', choices=('server', 'cloud', 'all'), default='server')
        if name != 'inspect':
            p.add_argument('--interface')
            p.add_argument('--gateway')
            p.add_argument('--ssh-policy', action='store_true', help='Linux server only: authorize a destination rule if main /32 alone is ineffective')
    p = sub.add_parser('apply')
    p.add_argument('--approve-plan', required=True)
    p = sub.add_parser('remove')
    p.add_argument('--scope', choices=('cloud', 'server', 'all'), default='all')
    p = sub.add_parser('ssh-egress', help='Measure public IPv4 using api.ipify.org over the physical SSH interface')
    p.add_argument('--save-for-plan', action='store_true', help='Record this measurement for ssh-recover --plan-source')
    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    # Do not chmod/chown an existing user's deployment when invoked through sudo.
    if not (state / 'deployment.json').is_file():
        raise ValueError('Use the existing private deployment state directory')
    if args.command == 'inspect':
        result = inspect(state, args.scope)
    elif args.command in ('plan', 'repair'):
        result = make_plan(state, args.scope, args.interface, args.gateway, args.ssh_policy)
    elif args.command == 'ssh-egress':
        result = measure_egress(state)
        if args.save_for_plan:
            save(state/'ssh-egress.json', {**result, 'measured_at': time.time(),
                                          'config': digest(read(state/'deployment.json'))})
    elif args.command == 'apply':
        result = apply(state, args.approve_plan)
    else:
        require_admin()
        with locked(state):
            groups = {'server': [], 'cloud': []} if args.scope == 'all' else {args.scope: []}
            result = reconcile(state, [], removing=True, groups=groups, all_scopes=args.scope == 'all')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('Network: ' + str(exc)) from None
