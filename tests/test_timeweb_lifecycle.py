import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import setup
import timeweb_lifecycle as lifecycle
import timeweb_setup as tw
import timeweb_update as upgrade
from timeweb_api import TimewebError


class API:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.lost_delete = False
        self.lost_publish = False
        self.ips = [{'id': 'ip-1', 'ip': '203.0.113.8', 'resource_id': 2}]
        self.deploys = []

    def request(self, path, method='GET', body=None):
        self.calls.append((method, path, body))
        if path == '/api/v1/floating-ips':
            return {'ips': copy.deepcopy(self.ips)}
        if path.endswith('/deploys'):
            return {'deploys': copy.deepcopy(self.deploys)}
        if method == 'POST' and path.endswith('/deploy'):
            self.deploys.append({'id': len(self.deploys)+1, 'status': 'building', 'commit_sha': body['commit_sha']})
            self.items['/api/v1/apps/1']['app']['commit_sha'] = body['commit_sha']
            if self.lost_publish:
                raise TimewebError(0, 'POST')
            return {}
        if method == 'DELETE':
            if path.endswith('/servers/2'):
                self.ips[0]['resource_id'] = None
            if path == '/api/v1/floating-ips/ip-1':
                self.ips = []
            self.items.pop(path, None)
            if self.lost_delete:
                self.lost_delete = False
                raise TimewebError(0, 'DELETE')
            return {}
        if path in self.items:
            return copy.deepcopy(self.items[path])
        for kind, (base, key) in lifecycle.RESOURCES.items():
            if path == base:
                plural = {'server': 'servers', 'app': 'apps', 'bucket': 'buckets'}.get(kind)
                if plural:
                    return {plural: [copy.deepcopy(v[key]) for k, v in self.items.items() if k.startswith(base+'/')]}
        if path.endswith('/resources'):
            return {k: [] for k in ('servers', 'balancers', 'buckets', 'clusters', 'databases', 'dedicated_servers')}
        raise TimewebError(404, method)


class LifecycleTests(unittest.TestCase):
    def test_live_project_categories_and_retained_resume(self):
        core = {k: [] for k in ('servers', 'balancers', 'buckets', 'clusters', 'databases', 'dedicated_servers')}
        extra = {k: [] for k in ('floating_ips', 'routers', 'network_drives', 'knowledgebases',
            'registries', 'monitorings', 'monitoring_alerts', 'cdn', 'agents', 'apps', 'domains', 'mailboxes')}
        path = '/api/v1/projects/4/resources'
        self.api.items[path] = {**core, **extra, 'meta': {'total': 1}, 'apps': [{'id': 42}]}
        plan = lifecycle.cleanup(self.state)
        first = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertIn('retained', first['steps']['project'])
        self.assertIn('/api/v1/projects/4', self.api.items)
        self.api.items[path] = {**core, **extra, 'meta': {'total': 0}, 'unknown_future_resource': []}
        second = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertIn('retained', second['steps']['project'])
        self.api.items[path] = {**core, **extra, 'meta': {'total': 1}}
        self.assertIn('retained', lifecycle.cleanup(self.state, plan['plan_sha256'])['steps']['project'])
        self.api.items[path] = {**core, **extra, 'meta': {'total': 0}}
        final = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertTrue(final['all_planned_resources_absent'])
        self.assertNotIn('retained', final['steps']['project'])

    def test_git_detail_is_repository_inventory_and_diagnostics_survives_bad_shape(self):
        api = API()
        api.items['/api/v1/vcs-provider/git1'] = {'repositories': [], 'meta': {'total': 0}}
        api.items['/api/v1/vcs-provider'] = {'providers': [{'provider_id': 'git1'}], 'meta': {'total': 1}}
        self.assertEqual(lifecycle.fetch(api, 'git', 'git1'), {'provider_id': 'git1'})
        api.items['/api/v1/vcs-provider']['meta']['total'] = 2
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            lifecycle.fetch(api, 'git', 'git1')
        self.api.items['/api/v1/apps/1'] = {'unexpected': []}
        report = lifecycle.diagnostics(self.state)['report']
        self.assertEqual(report['resources']['app']['observation_error'], 'unexpected_response_shape')
        self.assertTrue(report['resources']['server']['exists'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.api = API()
        self.config = {'mode': 'timeweb', 'name': 'pc-test', 'bucket': 'pc-test-backups', 'host': '203.0.113.8',
                       'source_commit': 'a'*40, 'source_branch': 'main', 'app_preset_id': 8}
        self.journal = {'config': tw.target(self.config), 'resources': {
            'app': {'id': 1}, 'server': {'id': 2}, 'bucket': {'id': 3}, 'project': {'id': 4}}, 'stage': 'complete'}
        for kind, ident in (('app', 1), ('server', 2), ('bucket', 3), ('project', 4)):
            base, key = lifecycle.RESOURCES[kind]
            self.api.items[base+'/'+str(ident)] = {key: {'id': ident, 'name': 'pc-test'+{'app': '-edge', 'bucket': '-backups'}.get(kind, ''),
                 'project_id': 4, 'commit_sha': 'a'*40, 'is_default': False, 'status': 'active',
                 'password': 'SECRET', 'envs': {'TOKEN': 'SECRET'}}}
        setup.save(self.state/'deployment.json', self.config)
        setup.save(self.state/'timeweb-journal.json', self.journal)
        patcher = patch('timeweb_lifecycle.API', return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preview_readonly_approval_and_lost_reply_resume(self):
        plan = lifecycle.cleanup(self.state)
        self.assertTrue(all(c[0] == 'GET' for c in self.api.calls))
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            lifecycle.cleanup(self.state, 'wrong')
        self.api.lost_delete = True
        with self.assertRaises(TimewebError):
            lifecycle.cleanup(self.state, plan['plan_sha256'])
        with self.assertRaisesRegex(ValueError, 'retired'):
            tw.approved(self.state)
        result = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertTrue(result['all_planned_resources_absent'])
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'DELETE' and c[1] == '/api/v1/apps/1']), 1)
        self.assertEqual(self.api.ips, [])
        self.assertTrue((self.state/'deployment.json').exists())

    def test_other_installation_and_shared_metadata_preserved(self):
        other = {'server': {'id': 99, 'name': 'other-memory', 'project_id': 99}}
        self.api.items['/api/v1/servers/99'] = other
        plan = lifecycle.cleanup(self.state)
        result = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertFalse(result['all_planned_resources_absent'])
        self.assertIn('retained', result['steps']['project'])
        self.assertEqual(self.api.items['/api/v1/servers/99'], other)

    def test_wrong_project_default_project_and_ambiguous_create_block(self):
        self.api.items['/api/v1/servers/2']['server']['project_id'] = 99
        with self.assertRaisesRegex(ValueError, 'project differs'):
            lifecycle.cleanup(self.state)
        self.api.items['/api/v1/servers/2']['server']['project_id'] = 4
        self.api.items['/api/v1/projects/4']['project']['is_default'] = True
        with self.assertRaisesRegex(ValueError, 'default project'):
            lifecycle.cleanup(self.state)
        self.journal['resources']['app'] = {'submitted': True}
        setup.save(self.state/'timeweb-journal.json', self.journal)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            lifecycle.cleanup(self.state)

    def test_ip_rebound_after_review_never_deleted(self):
        plan = lifecycle.cleanup(self.state)
        self.api.items.pop('/api/v1/servers/2')
        self.api.ips[0]['resource_id'] = 99
        with self.assertRaisesRegex(ValueError, 'another resource|IP still bound'):
            lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertFalse(any(c[0] == 'DELETE' and 'floating-ips' in c[1] for c in self.api.calls))

    def test_partial_inventory_blocks_cleanup(self):
        original = self.api.request
        def request(path, **kwargs):
            if path == '/api/v1/floating-ips':
                return {'ips': [], 'meta': {'total': 100}}
            return original(path, **kwargs)
        self.api.request = request
        with self.assertRaisesRegex(ValueError, 'Incomplete inventory'):
            lifecycle.cleanup(self.state)

    def test_support_bundle_has_no_provider_secrets(self):
        setup.save(self.state/'credentials.json', {'TIMEWEB_TOKEN': 'SECRET'})
        report = lifecycle.diagnostics(self.state)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertNotIn('envs', json.dumps(report))
        self.assertEqual(report['report']['resources']['app']['status'], 'active')

    def test_vm_rejection_diagnostics_keeps_correlation_not_raw_error(self):
        self.journal['resources']['server']={'submitted':False,'submitted_at':1795246138,
            'creation_rejection':{'status':400,'error_code':'servers_is_run_out_error'},
            'creation_attempts':[{'submitted_at':1795246138,'status':400,
                'error_code':'servers_is_run_out_error','response_id':'5a86f4e3-cb06-4ed5-b784-432119c3d092',
                'message':'SECRET'}]}
        setup.save(self.state/'timeweb-journal.json',self.journal)
        result=lifecycle.diagnostics(self.state)['report']
        self.assertEqual(result['vm_rejection']['cause'],'not_confirmed')
        self.assertIn('timeweb-select-vm',result['next'])
        self.assertEqual(result['vm_creation_attempts'][0]['submitted_at'],1795246138)
        self.assertNotIn('SECRET',json.dumps(result))

    def test_partial_deploy_history_is_reported_not_silently_complete(self):
        original = self.api.request
        def request(path, **kwargs):
            if path.endswith('/deploys'):
                return {'deploys': [{'id': 1, 'status': 'failed', 'commit_sha': 'a'*40}], 'meta': {'total': 200}}
            return original(path, **kwargs)
        self.api.request = request
        report = lifecycle.diagnostics(self.state)['report']['resources']['app']
        self.assertFalse(report['deploy_history_complete'])
        self.assertEqual(report['deploy_history_total'], 200)
        self.assertEqual(len(report['deploys']), 1)


class RemoteUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'memory').mkdir()
        with closing(sqlite3.connect(self.root/'memory/context.sqlite3')) as db:
            db.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
            db.execute("INSERT INTO metadata VALUES ('workspace_id','test-workspace')")
            db.commit()
        self.state = self.root/'gateway-rotation'
        self.state.mkdir()
        self.config = {'backend': 'timeweb', 'app_id': 1, 'app_name': 'pc-test-edge', 'source_commit': 'a'*40,
                       'public_host': 'test.twc1.net', 'cloud_state': str(self.state/'cloud')}
        setup.save(self.state/'config.json', self.config)
        self.api = API()
        self.api.items['/api/v1/apps/1'] = {'app': {'id': 1, 'name': 'pc-test-edge', 'commit_sha': 'a'*40,
              'domains': [{'fqdn': 'test.twc1.net', 'is_technical': True}], 'envs': {'TLS': 'SECRET'}}}
        self.payload = {'root': str(self.root), 'workspace_id': 'test-workspace', 'app_id': 1,
                        'old': 'a'*40, 'commit': 'b'*40, 'operation': 'test-op', 'review': 'first'}
        patcher = patch('timeweb_update.API', return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lost_publish_reply_resume_same_workspace_and_environment(self):
        self.api.lost_publish = True
        with self.assertRaises(TimewebError):
            upgrade.remote_step(self.payload)
        self.assertFalse(setup.read(self.state/'source-update.json')['complete'])
        self.api.deploys[0]['status'] = 'success'
        result = upgrade.remote_step(self.payload)
        self.assertEqual(result['workspace_id'], 'test-workspace')
        self.assertEqual(setup.read(self.state/'config.json')['source_commit'], 'b'*40)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'POST']), 1)
        self.assertFalse(any(c[0] == 'PATCH' for c in self.api.calls))
        # A later legitimate renewal may change env after the upgrade completed.
        self.api.items['/api/v1/apps/1']['app']['envs'] = {'TLS': 'NEW_SECRET'}
        self.assertEqual(upgrade.remote_step(self.payload)['status'], 'complete')

    def test_wrong_workspace_and_active_rotation_do_not_publish(self):
        with self.assertRaisesRegex(ValueError, 'Wrong memory'):
            upgrade.remote_step({**self.payload, 'workspace_id': 'other'})
        setup.save(self.state/'rotation.json', {'complete': False})
        with self.assertRaisesRegex(ValueError, 'renewal is in progress'):
            upgrade.remote_step(self.payload)
        self.assertEqual(self.api.calls, [])

    def test_retry_failed_only_once_and_never_while_running(self):
        self.assertEqual(upgrade.remote_step(self.payload)['status'], 'pending')
        retry = {**self.payload, 'retry_failed': True, 'review': 'retry-review'}
        with self.assertRaisesRegex(ValueError, 'still pending'):
            upgrade.remote_step(retry)
        self.api.deploys[0]['status'] = 'failed'
        self.assertEqual(upgrade.remote_step(retry)['status'], 'pending')
        self.assertEqual(upgrade.remote_step(retry)['status'], 'pending')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'POST']), 2)
        self.api.deploys[-1]['status'] = 'success'
        self.assertEqual(upgrade.remote_step(retry)['status'], 'complete')

    def test_renewal_fenced_before_backend_effects(self):
        from gateway_rotation import Rotation
        setup.save(self.state/'source-update.json', {'complete': False})
        # _tick checks the fence before accessing even active certificate files.
        controller = object.__new__(Rotation)
        controller.state = self.state
        self.assertEqual(controller._tick(True, None)['status'], 'busy')


class LocalUpdateTests(unittest.TestCase):
    setUp = LifecycleTests.setUp

    def prepare_update(self):
        self.journal['resources']['git'] = {'id': 'git-1'}
        body = {'name': 'pc-test-edge', 'comment': 'Portable Context HTTPS gateway', 'type': 'backend',
                'language': 'docker', 'framework': 'docker', 'provider_id': 'git-1', 'repository_id': 12,
                'branch_name': 'main', 'commit_sha': 'a'*40, 'build_cmd': '', 'run_cmd': '', 'is_auto_deploy': False,
                'preset_id': 8, 'build_entry_path': '/deploy/timeweb-edge', 'health_check': '/_pc/health', 'envs': {}}
        self.journal['resources']['app']['body_sha256'] = tw.digest(body)
        setup.save(self.state/'timeweb-journal.json', self.journal)
        self.api.items['/api/v1/apps/1']['app'].update(repository={'id': 12}, domains=[{'fqdn': 'test.twc1.net', 'is_technical': True}])
        for name, value in {'installed.json': {'root': '/tmp/memory', 'memory': {'workspace_id': 'workspace'}},
                            'https-installed.json': {}, 'rotation-installed.json': {},
                            'timeweb-plan.json': {'config': tw.target(self.config), 'plan_sha256': 'old'},
                            'timeweb-approved.json': {'plan_sha256': 'old'}}.items():
            setup.save(self.state/name, value)
        for name, value in [('timeweb_update.checkout', None), ('timeweb_update.API', self.api),
                            ('rotation_setup.refresh', {}), ('https_setup.check_public', {})]:
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_update_review_pending_and_memory_failure_resume(self):
        self.prepare_update()
        preview = upgrade.update(self.state, 'b'*40)
        self.assertFalse((self.state/'timeweb-update.json').exists())
        self.assertTrue(all(c[0] == 'GET' for c in self.api.calls))
        with patch('timeweb_update.call_remote', return_value={'status': 'pending'}) as remote:
            result = upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
            self.assertEqual(result['status'], 'pending')
        def interrupted(state, config):
            journal = setup.read(state/'timeweb-journal.json')
            journal['stage'] = 'memory'
            setup.save(state/'timeweb-journal.json', journal)
            raise RuntimeError('interrupted after file commit')
        with patch('timeweb_update.call_remote', return_value={'status': 'complete'}), patch('timeweb_setup.install_locked', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
        self.assertEqual(setup.read(self.state/'deployment.json')['source_commit'], 'b'*40)
        with patch('timeweb_update.call_remote') as remote, patch('timeweb_setup.install_locked', return_value={'workspace_id': 'workspace', 'checks': {'transport': True}}):
            result = upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
            self.assertTrue(result['updated'])
            remote.assert_not_called()
        self.assertTrue(upgrade.update(self.state, 'b'*40)['already_complete'])
        self.assertEqual(setup.read(self.state/'timeweb-journal.json')['resources']['server']['id'], 2)

    def test_change_after_preview_rejected_and_cleanup_can_supersede_stalled_upgrade(self):
        self.prepare_update()
        preview = upgrade.update(self.state, 'b'*40)
        with patch('timeweb_update.call_remote', return_value={'status': 'pending'}):
            upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
        self.api.items['/api/v1/apps/1']['app']['commit_sha'] = 'b'*40
        cleanup = lifecycle.cleanup(self.state)
        self.assertEqual(cleanup['abandons_pending_update'], preview['operation'])
        self.assertFalse(any(c[0] == 'DELETE' for c in self.api.calls))
        changed = setup.read(self.state/'deployment.json')
        changed['host'] = '203.0.113.99'
        setup.save(self.state/'deployment.json', changed)
        with self.assertRaisesRegex(ValueError, 'Local installation changed'):
            upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
        # Diagnostics still work during partial local journal transitions.
        report = lifecycle.diagnostics(self.state)
        self.assertNotIn('SECRET', json.dumps(report))

    def test_cleanup_after_failed_upgrade_retires_state_and_blocks_resume(self):
        self.prepare_update()
        preview = upgrade.update(self.state, 'b'*40)
        with patch('timeweb_update.call_remote', return_value={'status': 'pending'}):
            upgrade.update(self.state, 'b'*40, preview['plan_sha256'])
        self.api.items['/api/v1/apps/1']['app']['commit_sha'] = 'b'*40
        plan = lifecycle.cleanup(self.state)
        result = lifecycle.cleanup(self.state, plan['plan_sha256'])
        self.assertTrue(result['all_planned_resources_absent'])
        with self.assertRaisesRegex(ValueError, 'retired'):
            upgrade.update(self.state, 'b'*40, preview['plan_sha256'])


if __name__ == '__main__':
    unittest.main()
