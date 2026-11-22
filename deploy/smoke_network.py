"""CI only: exercise real OS host-route add/repair/remove against a TEST-NET address.

Run on disposable elevated runners, never against a personal deployment.
No traffic is sent to the test destination. Cleanup targets only this exact fixture route.
"""
import os
from pathlib import Path
import platform
import tempfile

import network


def main():
    if os.environ.get('CI') != 'true':
        raise SystemExit('This mutating smoke test is for disposable CI runners only')
    network.require_admin()
    system = platform.system()
    before = network.snapshot(system)
    destination = '198.51.100.77'
    assert not any(r['destination'] == destination for r in before['routes'])
    assert before['candidates'], before
    selected = before['candidates'][0]
    record = network.route_record(destination, **selected)
    with tempfile.TemporaryDirectory(prefix='pc route test ') as tmp:
        state = Path(tmp)
        network.save(state / 'deployment.json', {'mode': 'server', 'host': destination})
        try:
            plan = network.make_plan(state, 'server', **selected)
            network.apply(state, plan['plan_sha256'])
            assert record in network.snapshot(system)['routes']
            # A second fresh plan is idempotent.
            plan = network.make_plan(state, 'server', **selected)
            network.apply(state, plan['plan_sha256'])
            # Simulate the route disappearing on a restart, then restore through a fresh plan.
            network.run(network.command(system, 'delete', record))
            plan = network.make_plan(state, 'server', **selected)
            network.apply(state, plan['plan_sha256'])
            assert record in network.snapshot(system)['routes']
            network.reconcile(state, [], removing=True)
            assert record not in network.snapshot(system)['routes']
            if system == 'Linux':
                check_linux_policy(state, record)
            # Pre-existing routes must be reused, never claimed or removed.
            network.run(network.command(system, 'add', record))
            plan = network.make_plan(state, 'server', **selected)
            result = network.apply(state, plan['plan_sha256'])
            assert result['preexisting_reused'] == [record]
            network.reconcile(state, [], removing=True)
            assert record in network.snapshot(system)['routes']
        finally:
            if record in network.snapshot(system)['routes']:
                network.run(network.command(system, 'delete', record))
    print('Real route add, idempotency, repair, remove and pre-existing route protection passed:', system)


def check_linux_policy(state, record):
    # Model a higher-priority VPN lookup for only the test address. Send no packets.
    dest = record['destination'] + '/32'
    selector = ['priority', '12009', 'to', dest, 'lookup', '30523']
    assert all(row['priority'] != 12009 for row in network.linux_rules())
    network.run(['ip', '-4', 'route', 'add', dest, 'dev', 'lo', 'table', '30523'])
    try:
        network.run(['ip', '-4', 'rule', 'add', *selector])
        try:
            for _ in range(2):
                plan = network.make_plan(state, 'server', record['interface'], record['gateway'], ssh_policy=True)
                network.apply(state, plan['plan_sha256'])
                assert network.physical_path(network.tcp_route(record['destination'], 22, os.getuid()), record)
            assert len(network.read(state / 'network-routes.json')['ssh_rules']) == 1
            network.reconcile(state, [], removing=True, groups={'server': []})
            assert network.tcp_route(record['destination'], 22, os.getuid())['dev'] == 'lo'
        finally:
            network.run(['ip', '-4', 'rule', 'del', *selector])
    finally:
        network.run(['ip', '-4', 'route', 'del', dest, 'dev', 'lo', 'table', '30523'])


if __name__ == '__main__':
    main()
