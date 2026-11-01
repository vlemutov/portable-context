import importlib.util
import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from portable_context import backup
from portable_context.core import Store
from portable_context.workspace import Workspace

spec = importlib.util.spec_from_file_location('deployment_setup', Path(__file__).resolve().parents[1] / 'deploy/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def memory(self):
        store = Store(self.root / 'memory')
        ws = Workspace(store)
        store.ingest([{'source': 'test', 'instance': 'deploy', 'conversation_id': '1',
                       'message_id': '1', 'role': 'user', 'text': 'Preserve this decision.'}])
        raw = self.root / 'original.txt'
        raw.write_text('original archive')
        store.archive(raw)
        ws.refresh()
        return store, ws

    def test_discovery_reads_only_selected_project_and_does_not_store_secrets(self):
        project = '11111111-1111-4111-8111-111111111111'
        tenant = '22222222-2222-4222-8222-222222222222'
        replies = [io.BytesIO(json.dumps(v).encode()) for v in
                   [{'access_token': 'SECRET_TOKEN'},
                    {'id': project, 'tenant': {'id': tenant, 'status': 'ACTIVE'}}]]
        with patch.object(setup, 'command_env', return_value={'AUTH_KEY_ID': 'KEY', 'AUTH_SECRET': 'SECRET'}), \
                patch.object(setup.urllib.request, 'urlopen', side_effect=replies) as request:
            result = setup.discover(self.root, project)
        self.assertEqual(result, {'project_id': project, 'tenant_id': tenant})
        self.assertEqual(request.call_args_list[1].args[0].get_method(), 'GET')
        self.assertTrue(request.call_args_list[1].args[0].full_url.endswith('/projects/' + project))
        self.assertNotIn('SECRET', (self.root / 'discovery.json').read_text())

    def test_discovery_rejects_wrong_project_and_inactive_tenant(self):
        project = '11111111-1111-4111-8111-111111111111'
        for response in [{'id': 'another-project'},
                         {'id': project, 'tenant': {'id': project, 'status': 'SUSPEND'}}]:
            with self.subTest(response=response):
                replies = [io.BytesIO(json.dumps(v).encode()) for v in
                           [{'access_token': 'TOKEN'}, response]]
                with patch.object(setup, 'command_env', return_value={'AUTH_KEY_ID': 'KEY', 'AUTH_SECRET': 'SECRET'}), \
                        patch.object(setup.urllib.request, 'urlopen', side_effect=replies), \
                        self.assertRaises(ValueError):
                    setup.discover(self.root, project)
                self.assertFalse((self.root / 'discovery.json').exists())

    def test_snapshot_restore_keeps_identity_originals_and_manual_edits(self):
        store, ws = self.memory()
        (store.root / 'vault/manual.md').write_text('Uncommitted manual note')
        saved = backup.create(store.root, self.root / 'backups')
        restored = backup.restore(saved['path'], self.root / 'restored')
        other = Workspace(Store(restored['path']))
        self.assertEqual(other.describe()['workspace_id'], ws.describe()['workspace_id'])
        self.assertEqual(other.store.stats()['messages'], 1)
        self.assertEqual(other.store.stats()['originals'], 1)
        self.assertEqual((self.root / 'restored/recovered-vault/manual.md').read_text(), 'Uncommitted manual note')
        self.assertFalse((self.root / 'restored/vault/manual.md').exists())
        with self.assertRaises(ValueError):
            backup.restore(saved['path'], self.root / 'restored')

    def test_cloud_init_accepts_completed_warnings_but_rejects_failures(self):
        cases = [
            (0, {'status': 'done', 'errors': []}, True),
            (2, {'status': 'done', 'errors': [], 'recoverable_errors': {'DEPRECATED': ['old module']}}, True),
            (1, {'status': 'error', 'errors': ['package installation failed']}, False),
            (2, {'status': 'done', 'errors': ['failed']}, False),
            (0, {'status': 'running', 'errors': []}, False),
            (255, {}, False),
        ]
        for code, status, accepted in cases:
            with self.subTest(code=code, status=status), patch.object(setup.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], code, json.dumps(status))):
                if accepted:
                    setup.wait_cloud_init(['ssh'])
                else:
                    with self.assertRaises(ValueError):
                        setup.wait_cloud_init(['ssh'])

    def test_backup_does_not_copy_live_sqlite_or_uncommitted_transaction(self):
        store, _ = self.memory()
        with closing(sqlite3.connect(store.db)) as writer:
            writer.execute("UPDATE messages SET text='UNCOMMITTED'")
            saved = backup.create(store.root, self.root / 'backups')
            writer.rollback()
        restored = backup.restore(saved['path'], self.root / 'restore')
        with closing(sqlite3.connect(Path(restored['path']) / 'context.sqlite3')) as con:
            self.assertEqual(con.execute('SELECT text FROM messages').fetchone()[0], 'Preserve this decision.')

    def test_corrupt_original_fails_before_success(self):
        store, _ = self.memory()
        next((store.root / 'originals').iterdir()).write_text('corrupt')
        with self.assertRaises(ValueError):
            backup.create(store.root, self.root / 'backups')

    def test_restore_rejects_traversal_and_modified_payload(self):
        store, _ = self.memory()
        saved = backup.create(store.root, self.root / 'backups')
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('../escape', 'bad')
        with self.assertRaises(ValueError):
            backup.restore(bad, self.root / 'restore')
        with zipfile.ZipFile(saved['path']) as src, zipfile.ZipFile(bad, 'w') as dst:
            for name in src.namelist():
                dst.writestr(name, b'changed' if name == 'context.sqlite3' else src.read(name))
        with self.assertRaises(ValueError):
            backup.restore(bad, self.root / 'restore')
        self.assertFalse((self.root / 'restore').exists())

    def test_upload_requires_matching_remote_metadata(self):
        path = self.root / 'archive.zip'
        path.write_bytes(b'archive')
        from unittest.mock import Mock
        client = Mock()
        client.head_object.return_value = {'ContentLength': 7, 'Metadata': {'sha256': backup.sha256(path)}}
        with patch.object(backup, 's3_client', return_value=client):
            result = backup.upload(path, {'bucket': 'private'})
            self.assertEqual(result['key'], 'backups/archive.zip')
            client.head_object.return_value['Metadata']['sha256'] = 'wrong'
            with self.assertRaises(ValueError):
                backup.upload(path, {'bucket': 'private'})

    def test_init_resumes_same_deployment_and_rejects_mode_switch(self):
        first = setup.prepare(self.root, 'server')
        self.assertEqual(setup.prepare(self.root, 'server')['name'], first['name'])
        with self.assertRaises(ValueError):
            setup.prepare(self.root, 'cloudru')

    def test_reject_destructive_cloud_plan(self):
        with self.assertRaises(ValueError):
            setup.summarize_plan({'resource_changes': [{'address': 'disk.memory', 'change': {'actions': ['delete', 'create']}}]})
        self.assertEqual(setup.summarize_plan({'resource_changes': [{'address': 'disk.memory', 'change': {'actions': ['no-op']}}]}), [])

    def test_modified_plan_never_applies(self):
        (self.root / 'terraform').mkdir()
        (self.root / 'terraform/deployment.tfplan').write_bytes(b'changed')
        setup.save(self.root / 'plan-review.json', {'plan_sha256': 'approved', 'configuration_sha256': 'old'})
        with patch.object(setup, 'terraform') as tf, self.assertRaises(ValueError):
            setup.apply(self.root, 'approved')
        tf.assert_not_called()

    def test_config_change_invalidates_reviewed_plan(self):
        folder = self.root / 'terraform'
        folder.mkdir()
        (folder / 'main.tf').write_text('old')
        (folder / 'deployment.tfplan').write_bytes(b'plan')
        (self.root / 'terraform.rc').write_text('config')
        setup.save(self.root / 'deployment.json', {'disk_gb': 40})
        digest = setup.sha(folder / 'deployment.tfplan')
        setup.save(self.root / 'plan-review.json', {'plan_sha256': digest, 'configuration_sha256': setup.fingerprint(self.root)})
        setup.save(self.root / 'deployment.json', {'disk_gb': 80})
        with patch.object(setup, 'terraform') as tf, self.assertRaises(ValueError):
            setup.apply(self.root, digest)
        tf.assert_not_called()

    def test_shell_injection_in_host_rejected(self):
        config = setup.prepare(self.root, 'server')
        for host in ('-oProxyCommand=bad', 'example.org; touch /tmp/bad', 'user@host'):
            with self.assertRaises(ValueError):
                setup.validate_config(dict(config, host=host))

    def test_ssh_config_keeps_paths_with_spaces_out_of_option_arguments(self):
        folder = self.root / 'private state'
        folder.mkdir()
        key = folder / 'my key'
        key.write_text('TEST ONLY')
        config = setup.prepare(folder, 'server')
        config.update(host='192.0.2.1', identity_file=str(key), ssh_command='ssh')
        with patch.object(setup.shutil, 'which', return_value='/usr/bin/ssh'):
            args = setup.ssh_args(folder, config)
        self.assertEqual(args, ['ssh', '-T', '-F', str((folder / 'ssh-config').resolve()), 'portable-context'])
        text = (folder / 'ssh-config').read_text()
        self.assertIn('IdentityFile "' + key.resolve().as_posix() + '"', text)
        self.assertIn('StrictHostKeyChecking accept-new', text)
        self.assertIn('BatchMode yes', text)

    def test_bundle_is_repeatable_and_excludes_private_state(self):
        import base64
        import io
        first = setup.bundle()
        self.assertEqual(first['sha256'], setup.bundle()['sha256'])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(first['bundle']))) as archive:
            self.assertIn('src/portable_context/backup.py', archive.namelist())
            self.assertFalse(any(n.endswith(('.sqlite3', '.tfstate', '.json')) for n in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
