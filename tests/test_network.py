import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('network_helper', Path(__file__).resolve().parents[1] / 'deploy/network.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        network.save(self.state / 'deployment.json', {'mode': 'server', 'host': '198.51.100.77'})
        self.selected = {'interface': 'eth0', 'gateway': '192.168.1.1'}
        self.record = network.route_record('198.51.100.77', **self.selected)
        self.routes = []
        self.commands = []
        self.fail_add = False
        for name, value in [('snapshot', self.snapshot), ('run', self.mutate), ('require_admin', lambda: None)]:
            p = patch.object(network, name, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(network.platform, 'system', return_value='Linux')
        p.start()
        self.addCleanup(p.stop)

    def snapshot(self, system):
        return {'candidates': [self.selected], 'routes': list(self.routes)}

    def mutate(self, args):
        self.commands.append(args)
        record = network.route_record(args[4].removesuffix('/32'), args[6], args[8])
        if args[3] == 'add':
            if self.fail_add:
                raise RuntimeError('simulated failed route add')
            self.routes.append(record)
        else:
            self.routes.remove(record)
        return ''

    def plan(self):
        return network.make_plan(self.state, 'server')

    def test_routes_restore_after_reboot_and_remove_only_own(self):
        result = network.apply(self.state, self.plan()['plan_sha256'])
        self.assertEqual(result['owned'], [self.record])
        network.apply(self.state, self.plan()['plan_sha256'])
        self.assertEqual(len(self.commands), 1)
        self.routes.clear()
        network.apply(self.state, self.plan()['plan_sha256'])
        self.assertEqual(self.routes, [self.record])
        other = network.route_record('198.51.100.78', **self.selected)
        self.routes.append(other)
        network.reconcile(self.state, [], removing=True)
        self.assertEqual(self.routes, [other])

    def test_preexisting_route_not_adopted_or_removed(self):
        self.routes.append(self.record)
        result = network.apply(self.state, self.plan()['plan_sha256'])
        self.assertEqual(result['preexisting_reused'], [self.record])
        self.assertFalse(result['owned'])
        network.reconcile(self.state, [], removing=True)
        self.assertEqual(self.routes, [self.record])
        self.assertEqual(self.commands, [])

    def test_changed_gateway_replaces_only_previously_owned_route(self):
        network.apply(self.state, self.plan()['plan_sha256'])
        self.selected = {'interface': 'eth1', 'gateway': '10.0.0.1'}
        plan = self.plan()
        self.assertEqual(plan['remove_owned'], [self.record])
        network.apply(self.state, plan['plan_sha256'])
        self.assertEqual(self.routes, [network.route_record('198.51.100.77', **self.selected)])

    def test_stale_plan_and_conflicting_route_do_not_mutate(self):
        plan = self.plan()
        self.selected['gateway'] = '10.0.0.1'
        with self.assertRaises(ValueError):
            network.apply(self.state, plan['plan_sha256'])
        self.assertEqual(self.commands, [])
        self.routes.append(self.record)
        with self.assertRaises(ValueError):
            network.apply(self.state, self.plan()['plan_sha256'])
        self.assertEqual(self.commands, [])

    def test_tampered_expired_or_changed_deployment_plan_fails(self):
        plan = self.plan()
        saved = network.read(self.state / 'network-plan.json')
        saved['desired'][0]['destination'] = '203.0.113.9'
        network.save(self.state / 'network-plan.json', saved)
        with self.assertRaises(ValueError):
            network.apply(self.state, plan['plan_sha256'])
        with self.assertRaises(ValueError):
            network.apply(self.state, network.digest(saved))
        plan = self.plan()
        with patch.object(network.time, 'time', return_value=plan['created'] + 601), self.assertRaises(ValueError):
            network.apply(self.state, plan['plan_sha256'])
        network.save(self.state / 'deployment.json', {'mode': 'server', 'host': '203.0.113.9'})
        with self.assertRaises(ValueError):
            network.apply(self.state, plan['plan_sha256'])
        self.assertEqual(self.commands, [])

    def test_add_failure_is_resumable_and_interruption_does_not_guess_ownership(self):
        self.fail_add = True
        with self.assertRaises(RuntimeError):
            network.apply(self.state, self.plan()['plan_sha256'])
        self.assertNotIn('pending', network.read(self.state / 'network-routes.json'))
        self.fail_add = False
        network.apply(self.state, self.plan()['plan_sha256'])
        ledger = network.read(self.state / 'network-routes.json')
        ledger['pending'] = self.record
        network.save(self.state / 'network-routes.json', ledger)
        with self.assertRaises(ValueError):
            network.reconcile(self.state, [], removing=True)
        self.assertEqual(self.routes, [self.record])

    def test_external_route_change_does_not_get_deleted(self):
        network.apply(self.state, self.plan()['plan_sha256'])
        self.routes[0] = dict(self.record, gateway='10.0.0.1')
        with self.assertRaises(ValueError):
            network.reconcile(self.state, [], removing=True)
        self.assertEqual(self.routes[0]['gateway'], '10.0.0.1')

    def test_unsafe_destinations_and_interface_syntax_rejected(self):
        for ip in ('0.0.0.0', '127.0.0.1', '224.0.0.1', '169.254.169.254', '255.255.255.255', '::1'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                network.route_record(ip, **self.selected)
        with self.assertRaises(ValueError):
            network.route_record('198.51.100.77', '192.168.1.1', 'eth0; whoami')

    def test_parse_windows_macos_linux_tables(self):
        cases = [
            ('Windows', json.dumps([
                {'DestinationPrefix': '0.0.0.0/0', 'NextHop': '192.168.1.1', 'InterfaceIndex': 9, 'Physical': True},
                {'DestinationPrefix': '0.0.0.0/0', 'NextHop': '10.8.0.1', 'InterfaceIndex': 10, 'Physical': False},
                {'DestinationPrefix': '198.51.100.77/32', 'NextHop': '192.168.1.1', 'InterfaceIndex': 9}]), '9'),
            ('Darwin', 'Routing tables\nInternet:\nDestination Gateway Flags Netif Expire\n'
             'default 192.168.1.1 UGScg en0\ndefault 10.8.0.1 UGSc utun3\n'
             '198.51.100.77 192.168.1.1 UGHS en0\n127.0.0.1 127.0.0.1 UH lo0\n', 'en0'),
            ('Linux', json.dumps([{'dst': 'default', 'gateway': '192.168.1.1', 'dev': 'eth0'},
                                 {'dst': 'default', 'gateway': '10.8.0.1', 'dev': 'wg0'},
                                 {'dst': '198.51.100.77', 'gateway': '192.168.1.1', 'dev': 'eth0'}]), 'eth0'),
        ]
        for system, text, dev in cases:
            with self.subTest(system=system):
                result = network.parse_routes(system, text)
                self.assertEqual(result['candidates'], [{'interface': dev, 'gateway': '192.168.1.1'}])
                self.assertEqual(result['routes'], [network.route_record('198.51.100.77', '192.168.1.1', dev)])


if __name__ == '__main__':
    unittest.main()
