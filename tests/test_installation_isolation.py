"""Two owner installations in different projects, without calling a cloud API."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import setup
import https_setup
from cloud_gateway import CloudAPI
from portable_context.core import Store
from portable_context.workspace import Workspace


class InstallationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def new(self, name, project):
        state = self.root / name
        state.mkdir()
        setup.prepare(state, 'cloudru')
        setup.configure(state, {'project_id': project, 'tenant_id': project, 'image_id': project,
                                'zone': 'test-zone', 'flavor': 'test-flavor', 'ssh_cidr': '192.0.2.1/32'})
        return state

    def test_two_projects_render_separate_targets_and_keep_first_unchanged(self):
        first = self.new('first', '11111111-1111-4111-8111-111111111111')
        second = self.new('second', '22222222-2222-4222-8222-222222222222')
        def key(state):
            target = state / 'id_ed25519'
            target.write_text('synthetic private key ' + state.name)
            target.with_suffix('.pub').write_text('synthetic public key ' + state.name)
            return target
        with patch.object(setup, 'ensure_key', side_effect=key):
            setup.render(first, setup.read(first / 'deployment.json'))
            before = {p.relative_to(first): p.read_bytes() for p in first.rglob('*') if p.is_file()}
            setup.render(second, setup.read(second / 'deployment.json'))
        self.assertEqual(before, {p.relative_to(first): p.read_bytes() for p in first.rglob('*') if p.is_file()})
        a, b = [setup.read(p / 'terraform/deployment.auto.tfvars.json') for p in (first, second)]
        for field in ('project_id', 'tenant_id', 'name', 'bucket', 'ssh_public_key'):
            self.assertNotEqual(a[field], b[field])
        self.assertEqual(setup.prepare(first, 'cloudru')['name'], a['name'])
        self.assertEqual(setup.command_env(first, False)['TF_CLI_CONFIG_FILE'], str(first / 'terraform.rc'))
        self.assertNotEqual(Workspace(Store(first / 'memory')).describe()['workspace_id'],
                            Workspace(Store(second / 'memory')).describe()['workspace_id'])

    def test_configure_cannot_retarget_used_state_and_does_not_write_on_failure(self):
        state = self.new('first', '11111111-1111-4111-8111-111111111111')
        original = (state / 'deployment.json').read_bytes()
        for evidence in ('plan-review.json', 'terraform/deployment.tfplan', 'terraform/terraform.tfstate',
                         'installed.json', 'https-plan.json', 'https-journal.json'):
            artifact = state / evidence
            artifact.parent.mkdir(exist_ok=True)
            artifact.write_text('{}')
            for values in ({'project_id': '22222222-2222-4222-8222-222222222222'},
                           {'tenant_id': '22222222-2222-4222-8222-222222222222'},
                           {'bucket': 'other-memory-bucket'}, {'host': '192.0.2.99'},
                           {'backup_repository': 's3:https://s3.example/other'}):
                with self.subTest(evidence=evidence, fields=list(values)):
                    with self.assertRaisesRegex(ValueError, 'new private state directory'):
                        setup.configure(state, values)
                    self.assertEqual((state / 'deployment.json').read_bytes(), original)
            artifact.unlink()

    def test_same_target_and_network_update_remain_allowed(self):
        state = self.new('first', '11111111-1111-4111-8111-111111111111')
        setup.save(state / 'installed.json', {})
        setup.configure(state, {'project_id': '11111111-1111-4111-8111-111111111111',
                                'ssh_cidr': '198.51.100.2/32', 'disk_gb': 60})
        config = setup.read(state / 'deployment.json')
        self.assertEqual(config['ssh_cidr'], '198.51.100.2/32')
        self.assertEqual(config['disk_gb'], 60)

    def test_parent_terraform_state_and_arguments_cannot_leak(self):
        inherited = {'TF_DATA_DIR': '/other/.terraform', 'TF_WORKSPACE': 'production',
                     'TF_CLI_ARGS': '-state=/other/live.tfstate', 'TF_CLI_ARGS_apply': '-auto-approve',
                     'TF_VAR_project_id': 'other', 'TF_LOG': 'TRACE', 'TF_LOG_PATH': '/other/log',
                     'TF_CLI_CONFIG_FILE': '/other/terraform.rc'}
        with patch.dict(os.environ, inherited):
            env = setup.command_env(self.root, credentials=False)
        for name in inherited:
            if name != 'TF_CLI_CONFIG_FILE':
                self.assertNotIn(name, env)
        self.assertEqual(env['TF_CLI_CONFIG_FILE'], str(self.root / 'terraform.rc'))
        self.assertEqual(env['TF_IN_AUTOMATION'], '1')

    def test_https_rejects_cross_project_before_terraform_or_cloud(self):
        state = self.new('first', '11111111-1111-4111-8111-111111111111')
        with patch.object(https_setup, 'terraform') as terraform, patch.object(https_setup, 'CloudAPI') as cloud:
            with self.assertRaisesRegex(ValueError, 'HTTPS project must match'):
                https_setup.plan(state, {'project_id': '22222222-2222-4222-8222-222222222222'})
            terraform.assert_not_called()
            cloud.assert_not_called()
        self.assertFalse((state / 'https-firewall').exists())

    def test_api_rejects_cross_project_override_before_request(self):
        api = CloudAPI.__new__(CloudAPI)
        api.project_id = '11111111-1111-4111-8111-111111111111'
        api.token = 'synthetic'
        with patch.object(api, '_request') as request:
            for keyword in ('query', 'body'):
                with self.assertRaisesRegex(ValueError, 'another project'):
                    api.request('containers', '/v2/containers', **{keyword: {'projectId': 'other'}})
            request.assert_not_called()
            api.request('containers', '/v2/containers', query={'pageSize': 10})
            query = parse_qs(urlsplit(request.call_args.args[0]).query)
            self.assertEqual(query['projectId'], [api.project_id])
            self.assertEqual(query['pageSize'], ['10'])

    def test_journals_do_not_adopt_another_project(self):
        first = self.root / 'first'; first.mkdir()
        second = self.root / 'second'; second.mkdir()
        a = {'project_id': 'first', 'container_name': 'pc-first-edge'}
        b = {'project_id': 'second', 'container_name': 'pc-second-edge'}
        ja = https_setup.Journal(first, a)
        ja.stage('container', lambda intent: {'id': 'first-container', 'origin': 'https://first.example'})
        before = ja.path.read_bytes()
        jb = https_setup.Journal(second, b)
        self.assertEqual(jb.value['completed'], {})
        with self.assertRaisesRegex(ValueError, 'another target'):
            https_setup.Journal(first, b)
        self.assertEqual(ja.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
