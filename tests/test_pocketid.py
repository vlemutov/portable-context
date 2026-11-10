import importlib.util
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
