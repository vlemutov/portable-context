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
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import time

CLOUD_HOSTS = ('console.cloud.ru', 'iam.api.cloud.ru', 'compute.api.cloud.ru',
               'vpc.api.cloud.ru', 'organization.api.cloud.ru', 's3.cloud.ru')
METRIC = 17383


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, data):
    tmp = path.with_suffix('.new')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    if os.name != 'nt' and os.geteuid() == 0:
        owner = path.parent.stat()
        os.chown(tmp, owner.st_uid, owner.st_gid)
    tmp.replace(path)


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


def targets(config, scope):
    endpoints = []
    if scope in ('cloud', 'all'):
        if config['mode'] != 'cloudru':
            raise ValueError('Cloud endpoints apply only to a cloudru deployment')
        endpoints.extend((host, 443) for host in CLOUD_HOSTS)
    if scope in ('server', 'all'):
        if not config.get('host'):
            raise ValueError('Server not created yet; use --scope cloud first')
        endpoints.append((config['host'], int(config.get('ssh_port', 22))))
    resolved = []
    for host, port in endpoints:
        addresses = sorted({ipv4(row[4][0]) for row in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
        if not addresses:
            raise ValueError('No IPv4 addresses for ' + host)
        resolved.append({'host': host, 'port': port, 'addresses': addresses})
    return resolved


def probe(endpoint):
    results = []
    for ip in endpoint['addresses']:
        try:
            with socket.create_connection((ip, endpoint['port']), timeout=3):
                results.append({'address': ip, 'tcp': 'reachable'})
        except OSError as exc:
            results.append({'address': ip, 'tcp': 'failed', 'reason': str(exc)})
    return dict(endpoint, results=results)


def inspect(state, scope):
    system = platform.system()
    network = snapshot(system)
    endpoints = targets(read(state / 'deployment.json'), scope)
    with ThreadPoolExecutor(max_workers=6) as pool:
        checks = list(pool.map(probe, endpoints))
    return {'os': system, **network, 'endpoints': checks,
            'note': 'TCP only; no proof of country, TLS, credentials or SSH authorization. VPN policy routing/kill switches may override routes.'}


def make_plan(state, scope, interface=None, gateway=None):
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
    endpoints = targets(config, scope)
    desired = [route_record(ip, **selected) for ip in sorted({ip for e in endpoints for ip in e['addresses']})]
    ledger = read(state / 'network-routes.json') if (state / 'network-routes.json').exists() else None
    plan = {'format': 1, 'os': system, 'created': time.time(), 'configuration': digest(config),
            'ownership': digest(ledger), 'remove_owned': [r for r in (ledger or {}).get('owned', []) if r not in desired],
            'scope': scope, 'endpoints': endpoints, 'desired': desired}
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


def reconcile(state, desired, *, removing=False):
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
    for old in list(ledger['owned']):
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
    if removing:
        save(ledger_path, ledger)
    return {'owned': ledger['owned'], 'preexisting_reused': reused,
            'next': 'Run inspect and setup.py check. Routes are session-scoped; after reboot/network change generate and apply a fresh plan.'}


def apply(state, approval):
    with locked(state):
        plan = read(state / 'network-plan.json')
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
        expected = targets(read(state / 'deployment.json'), plan['scope'])
        if expected != plan['endpoints']:
            raise ValueError('DNS changed; regenerate the plan')
        if {r['destination'] for r in plan['desired']} != {ip for e in expected for ip in e['addresses']}:
            raise ValueError('Plan destinations do not match the deployment endpoints')
        require_admin()
        return reconcile(state, plan['desired'])


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
    p = sub.add_parser('apply')
    p.add_argument('--approve-plan', required=True)
    sub.add_parser('remove')
    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    # Do not chmod/chown an existing user's deployment when invoked through sudo.
    if not (state / 'deployment.json').is_file():
        raise ValueError('Use the existing private deployment state directory')
    if args.command == 'inspect':
        result = inspect(state, args.scope)
    elif args.command in ('plan', 'repair'):
        result = make_plan(state, args.scope, args.interface, args.gateway)
    elif args.command == 'apply':
        result = apply(state, args.approve_plan)
    else:
        require_admin()
        with locked(state):
            result = reconcile(state, [], removing=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('Network: ' + str(exc)) from None
