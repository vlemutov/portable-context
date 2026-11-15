import importlib.util
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pocketid_bootstrap', Path(__file__).resolve().parents[1] / 'deploy/pocketid.py')
pocketid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pocketid)


class OwnerEnrollmentTests(unittest.TestCase):
    def test_code_fits_no_email_login_form(self):
        api = pocketid.Bootstrap(14120, 'synthetic-key')
        owner = '00000000-0000-4000-8000-000000000001'
        # Provider v2.19.2 uses six characters for TTL <=15m; UI needs 12.
        def provider(path, data):
            self.assertEqual(path, '/api/users/'+owner+'/one-time-access-token')
            self.assertEqual(data['ttl'], '20m')
            return {'token': 'ABCD2345EFGH'}
        with patch.object(api, 'request', side_effect=provider):
            self.assertEqual(len(api.create_owner_login_code(owner)), 12)

    def test_incompatible_code_is_not_exposed(self):
        api = pocketid.Bootstrap(14120, 'synthetic-key')
        with patch.object(api, 'request', return_value={'token': 'ABC123'}):
            with self.assertRaisesRegex(RuntimeError, 'value suppressed') as error:
                api.create_owner_login_code('00000000-0000-4000-8000-000000000001')
            self.assertNotIn('ABC123', str(error.exception))


class DesktopCallbackTests(unittest.TestCase):
    def setUp(self):
        self.api = pocketid.Bootstrap(14120, 'synthetic-key')
        self.receipt = {'clients': ['portable-context-desktop']}
        self.client = {'id': 'portable-context-desktop', 'clientType': 'standard',
            'name': 'Owner customized name', 'description': 'Keep this',
            'callbackURLs': [pocketid.CODEX_CALLBACK, 'http://127.0.0.1:14882/other'],
            'isPublic': True, 'pkceEnabled': True, 'skipConsent': False,
            'isGroupRestricted': True, 'allowedUserGroups': [{'id': 'owned-group'}],
            'requiresReauthentication': True, 'requiresPushedAuthorizationRequests': False,
            'credentials': {'federatedIdentities': []}, 'logoutCallbackURLs': ['https://example.org/logout'],
            'accessTokenDurationMinutes': 7, 'refreshTokenDurationMinutes': 30}
        self.writes = 0
        self.backups = 0

    def provider(self, path, data=None, method=None):
        self.assertEqual(path, '/api/oidc/clients/portable-context-desktop')
        if method == 'PUT':
            self.assertEqual(self.backups, 1)
            self.writes += 1
            self.client = copy.deepcopy(data)
        else:
            self.assertIsNone(method)
        return copy.deepcopy(self.client)

    def backup(self):
        self.backups += 1

    def test_additive_checkpointed_migration_is_idempotent_and_preserves_owner_settings(self):
        before = copy.deepcopy(self.client)
        with patch.object(self.api, 'request', side_effect=self.provider):
            result = self.api.enable_claude_code(self.receipt, self.backup)
            self.assertTrue(result['changed'])
            self.assertFalse(self.api.enable_claude_code(self.receipt, self.backup)['changed'])
        self.assertEqual((self.writes, self.backups), (1, 1))
        self.assertEqual(self.client.pop('callbackURLs'), [*before.pop('callbackURLs'), pocketid.CLAUDE_CODE_CALLBACK])
        self.assertEqual(self.client, before)

    def test_failed_checkpoint_or_concurrent_edit_prevents_put(self):
        def failed(): raise RuntimeError('Backup unavailable')
        with patch.object(self.api, 'request', side_effect=self.provider):
            with self.assertRaisesRegex(RuntimeError, 'Backup unavailable'):
                self.api.enable_claude_code(self.receipt, failed)
            def concurrent(): self.client['name'] = 'New owner edit'
            with self.assertRaisesRegex(ValueError, 'Client changed'):
                self.api.enable_claude_code(self.receipt, concurrent)
        self.assertEqual(self.writes, 0)

    def test_rejects_unowned_or_changed_security_profile(self):
        with patch.object(self.api, 'request', side_effect=self.provider):
            with self.assertRaisesRegex(ValueError, 'not owned'):
                self.api.enable_claude_code({}, self.backup)
            for field, bad in [('isPublic', False), ('pkceEnabled', False), ('skipConsent', True),
                               ('clientType', 'cimd'), ('callbackURLs', ['https://example.org/new'])]:
                previous = self.client[field]; self.client[field] = bad
                with self.assertRaises(ValueError): self.api.enable_claude_code(self.receipt, self.backup)
                self.client[field] = previous
        self.assertEqual((self.writes, self.backups), (0, 0))

    def test_lost_success_reply_resumes_without_second_mutation(self):
        def lost(path, data=None, method=None):
            value = self.provider(path, data, method)
            if method == 'PUT': raise RuntimeError('Lost reply')
            return value
        with patch.object(self.api, 'request', side_effect=lost):
            with self.assertRaisesRegex(RuntimeError, 'Lost reply'):
                self.api.enable_claude_code(self.receipt, self.backup)
            self.assertFalse(self.api.enable_claude_code(self.receipt, self.backup)['changed'])
        self.assertEqual((self.writes, self.backups), (1, 1))
