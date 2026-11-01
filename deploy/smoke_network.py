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


if __name__ == '__main__':
    main()
