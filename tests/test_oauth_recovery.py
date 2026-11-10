import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]/'deploy'
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location('oauth_recovery', DEPLOY/'oauth_recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root/'oauth'
        auth = self.runtime/'identity'; (auth/'data').mkdir(parents=True)
        self.binary = auth/'pocket-id'; self.binary.write_bytes(b'synthetic binary')
        patched = patch.object(recovery, 'LINUX_AMD64_SHA256', recovery.checksum(self.binary))
        patched.start(); self.addCleanup(patched.stop)
        self.origin = 'https://memory.example'
        self.owner = '00000000-0000-4000-8000-000000000001'
        recovery.write(auth/'environment.json', {'APP_URL': self.origin, 'HOST': '127.0.0.1', 'PORT': '14120',
           'UI_CONFIG_DISABLED': 'true', 'ALLOW_USER_SIGNUPS': 'disabled',
           'ENCRYPTION_KEY': 'old-secret', 'STATIC_API_KEY': 'old-api-key'})
        recovery.write(auth/'receipt.json', {'origin': self.origin, 'owner_subject': self.owner})
        recovery.write(self.runtime/'oauth.json', {'issuer': self.origin, 'resource': self.origin+'/mcp',
            'provider_origin': 'http://127.0.0.1:14120', 'owner_subject': self.owner})
        self.db = sqlite3.connect(auth/'data/pocket-id.db')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE sessions(token TEXT)')
        self.db.execute("INSERT INTO sessions VALUES ('old-revoked-session')"); self.db.commit()
        self.memory_config = self.root/'memory-config.json'
        password = self.root/'password'; password.write_text('synthetic-restic-password')
        recovery.write(self.memory_config, {'home': str(self.root/'memory'), 'state': str(self.root/'memory-state'),
           'binary': os.environ.get('PC_TEST_RESTIC', 'restic'), 'password_file': str(password),
           'repository': str(self.root/'memory-repository')})
        self.config = self.root/'private/identity-config.json'
        recovery.configure(self.runtime, self.memory_config, self.config, str(self.root/'identity-repository'))
        self.manager = recovery.IdentityBackups(self.config)

    def test_separate_repository_and_configuration_guard(self):
        for repo in [str(self.root/'memory-repository'), str(self.root/'memory-repository/nested')]:
            with self.assertRaisesRegex(ValueError, 'separate|sibling'):
                recovery.configure(self.runtime, self.memory_config, self.config, repo)
        env = recovery.read(self.runtime/'identity/environment.json')
        env['DB_CONNECTION_STRING'] = 'postgres://not-supported'
        recovery.write(self.runtime/'identity/environment.json', env)
        with self.assertRaisesRegex(ValueError, 'SQLite'):
            recovery.runtime_info(self.runtime)

    def test_owner_code_file_never_printed_or_overwritten(self):
        target = self.root/'handoff/code.json'
        with patch.object(recovery.Bootstrap, 'create_owner_login_code', return_value='ABCD2345EFGH'):
            result = recovery.owner_login(self.runtime, target)
            self.assertNotIn('ABCD2345EFGH', json.dumps(result))
            self.assertEqual(recovery.read(target)['code'], 'ABCD2345EFGH')
            with self.assertRaisesRegex(ValueError, 'new private'):
                recovery.owner_login(self.runtime, target)

    @unittest.skipUnless(os.environ.get('PC_TEST_RESTIC'), 'Set PC_TEST_RESTIC for encrypted round trip')
    def test_real_encrypted_round_trip_wal_and_fresh_identity(self):
        self.manager.initialize()
        first = self.manager.backup()
        self.assertEqual(self.manager.backup()['status'], 'unchanged')
        # Authentication-only changes must create a snapshot even when memory is untouched.
        self.db.execute("INSERT INTO sessions VALUES ('second-session')"); self.db.commit()
        second = self.manager.backup()
        self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])
        target = self.root/'quarantine'
        self.manager.restore(second['snapshot_id'], target)
        with closing(sqlite3.connect((target/'pocket-id.db').as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 2)
        fresh = self.root/'fresh'
        recovery.prepare_recovery(target, fresh, self.binary, self.origin, 14130)
        env = recovery.read(fresh/'identity/environment.json')
        self.assertNotEqual(env['ENCRYPTION_KEY'], 'old-secret')
        self.assertNotEqual(env['STATIC_API_KEY'], 'old-api-key')
        self.assertFalse((fresh/'identity/data').exists())
        self.assertFalse((fresh/'oauth.json').exists())
        self.assertFalse(recovery.read(fresh/'recovery.json')['activation_allowed'])
        with self.assertRaisesRegex(ValueError, 'issuer mismatch'):
            recovery.prepare_recovery(target, self.root/'wrong', self.binary, 'https://other.example')
        (target/'environment.json').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            recovery.prepare_recovery(target, self.root/'tampered', self.binary, self.origin)
        self.assertFalse(any(self.manager.state.glob('identity-stage-*')))
        self.assertTrue(self.manager.check()['verified'])
        self.assertFalse(self.manager.prune()['removed_snapshots'])
        kit = self.root/'offline-kit'
        recovery.export_kit(self.config, kit)
        self.db.close(); self.runtime.rename(self.root/'lost-server-runtime')
        newconfig = self.root/'new-server/identity-backup-config.json'
        recovery.restore_config(kit, newconfig, self.root/'new-server/oauth', self.root/'new-server/memory',
                                os.environ['PC_TEST_RESTIC'], self.origin)
        restored = recovery.IdentityBackups(newconfig).restore(second['snapshot_id'], self.root/'after-server-loss')
        self.assertTrue(restored['verified'])
        with self.assertRaisesRegex(ValueError, 'issuer mismatch'):
            recovery.restore_config(kit, self.root/'wrong-config', self.root/'x', self.root/'y',os.environ['PC_TEST_RESTIC'],'https://other.example')

    def test_mixed_repository_and_live_restore_rejected(self):
        with patch.object(self.manager.runner, 'run', return_value='[{"id":"other","tags":[]}]'):
            with self.assertRaisesRegex(ValueError, 'unrelated'):
                self.manager.prune()
        for target in [self.runtime, self.runtime/'nested', self.manager.state/'nested', self.root]:
            with self.assertRaisesRegex(ValueError, 'new directory'):
                self.manager.restore('a'*64, target)

    def test_scheduler_preserves_memory_jobs_and_is_idempotent(self):
        memory = '17 3 * * * /private/backup # portable-context-managed-backup\n'
        paths = [self.root/x for x in ('script.py', 'python', 'config.json', 'state')]
        first = recovery.cron_lines(memory, *paths)
        self.assertEqual(recovery.cron_lines(first, *paths), first)
        self.assertIn(memory, first)
        self.assertEqual(first.count('# portable-context-identity-'), 3)

    def test_process_environment_and_mcp_status_do_not_expose_secrets(self):
        from portable_context.restic_backup import oauth_backup_status
        with patch.dict(os.environ, {'DB_CONNECTION_STRING': 'live.db', 'STATIC_API_KEY_FILE': '/old/key'}):
            env = recovery.provider_environment({'HOST': '127.0.0.1'})
            self.assertNotIn('DB_CONNECTION_STRING', env)
            self.assertNotIn('STATIC_API_KEY_FILE', env)
        recovery.write(self.root/'identity-backup-config.json', {'STATIC_API_KEY': 'must-not-read'})
        recovery.write(self.root/'identity-backup-state/status.json', {'at': 'test', 'snapshot_id': 'id', 'secret': 'must-not-show'})
        result = oauth_backup_status(self.root/'memory')
        self.assertTrue(result['configured'])
        self.assertNotIn('must-not', json.dumps(result))

    def test_activation_failure_keeps_http_closed_without_restoring_old_identity(self):
        import subprocess
        target = self.root/'candidate'; auth = target/'identity'; auth.mkdir(parents=True)
        (auth/'pocket-id').write_bytes(self.binary.read_bytes())
        env = recovery.read(self.runtime/'identity/environment.json')
        env.update(PORT='14130', ENCRYPTION_KEY='fresh-secret', STATIC_API_KEY='fresh-api')
        recovery.write(auth/'environment.json', env)
        newowner = '00000000-0000-4000-8000-000000000002'
        recovery.write(auth/'receipt.json', {'origin': self.origin, 'owner_subject': newowner})
        oauth = recovery.read(self.runtime/'oauth.json');oauth.update(owner_subject=newowner, provider_origin='http://127.0.0.1:14130')
        recovery.write(target/'oauth.json', oauth)
        recovery.write(target/'recovery.json', {'state':'bootstrapped','previous_owner_subject':self.owner,
            'new_owner_subject':newowner,'environment_sha256':recovery.checksum(auth/'environment.json')})
        calls=[]
        real_run = recovery.subprocess.run
        def service(args, **kwargs):
            if args[0] != "systemctl": return real_run(args, **kwargs)
            if args[2] == 'show':
                return subprocess.CompletedProcess(args,0,stdout=str(self.runtime/'identity/run.py')+' '+str(self.runtime/'oauth.json'))
            calls.append(args[2:])
            if args[2:] == ['start','pc-identity.service']:
                raise subprocess.CalledProcessError(1,args)
            return subprocess.CompletedProcess(args,0)
        # Close the test's WAL connection before simulating a stopped provider on Windows.
        self.db.close()
        with patch('sys.platform','linux'), patch.object(recovery.IdentityBackups,'backup',return_value={'snapshot_id':'before'}), patch.object(recovery.subprocess,'run',side_effect=service):
            with self.assertRaises(subprocess.CalledProcessError):
                recovery.activate_recovery(target,self.config,self.owner,'RESET ACCESS '+self.origin)
        self.assertEqual(calls[:2],[['stop','pc-mcp-http.service'],['stop','pc-identity.service']])
        self.assertEqual(calls[-1],['stop','pc-mcp-http.service'])
        self.assertEqual(recovery.read(self.runtime/'oauth.json')['owner_subject'],newowner)
        self.assertEqual(recovery.read(self.runtime/'identity/environment.json')['ENCRYPTION_KEY'],'fresh-secret')
        self.assertTrue(list(self.runtime.glob('identity-retired-*')))
        self.assertTrue(recovery.read(self.manager.state/'activation-failed.json')['http_stopped'])

    def test_lost_runtime_never_replaces_an_existing_identity(self):
        target = self.root/'candidate'; target.mkdir()
        recovery.write(target/'recovery.json', {'state':'bootstrapped'})
        with patch('sys.platform','linux'), patch.object(recovery,'runtime_info',return_value=recovery.runtime_info(self.runtime)):
            with self.assertRaisesRegex(ValueError,'requires absent'):
                recovery.activate_recovery(target,self.config,self.owner,'RESET ACCESS '+self.origin,lost_runtime=True)
        self.assertTrue((self.runtime/'identity/data/pocket-id.db').is_file())

