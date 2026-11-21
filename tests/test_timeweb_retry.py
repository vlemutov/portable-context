import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
from setup import read, save
from timeweb_api import TimewebError
from timeweb_setup import Journal, digest
from timeweb_retry import retry_create


class API:
    def __init__(self):
        self.rows = []
        self.posts = 0
        self.lost = False
        self.total = None
        self.late = False

    def request(self, path, method='GET', body=None):
        if method == 'POST':
            self.posts += 1
            if self.lost:
                raise TimewebError(0, 'POST')
            self.rows.append({'id': 1, **body})
            if self.late:
                self.rows.append({'id': 2, **body})
            return {'app': {'id': 1}}
        if path == '/api/v1/apps':
            return {'apps': copy.deepcopy(self.rows), 'meta': {'total': len(self.rows) if self.total is None else self.total}}
        return {'app': self.rows[0]}


class RetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.config = {'mode': 'timeweb', 'name': 'pc-test'}
        self.body = {'name': 'pc-test-edge', 'provider_id': 'git1'}
        self.api = API()
        save(self.state/'deployment.json', self.config)
        save(self.state/'timeweb-journal.json', {'config': self.config, 'resources': {
            'git': {'id': 'git1'}, 'app': {'id': None, 'submitted': True, 'path': '/api/v2/apps',
                                          'key': 'app', 'body_sha256': digest(self.body)}}})
        p = patch('timeweb_retry.API', return_value=self.api)
        p.start()
        self.addCleanup(p.stop)

    def approve(self):
        plan = retry_create(self.state)
        return retry_create(self.state, plan['plan_sha256'])

    def create(self, body=None):
        return Journal(self.state, self.config).resource(self.api, 'app', '/api/v2/apps', 'app', body or self.body, lambda v: True)

    def legacy_refusals(self):
        path = self.state/'timeweb-journal.json'
        journal = read(path)
        journal['config']['app_preset_id'] = 1003
        save(self.state/'deployment.json', journal['config'])
        app = journal['resources']['app']
        for i in range(3):
            if i:
                previous = copy.deepcopy({k: v for k, v in app.items() if k != 'retry_history'})
                app.setdefault('retry_history', []).append({'previous': previous})
            app['submitted_at'] = 100 + i
            app.setdefault('creation_attempts', []).append(
                {'submitted_at': 100+i, 'status': 409, 'error_code': 'no_free_node'})
        save(path, journal)
        return journal

    def test_old_refusals_allow_cleanup_preview_but_timeout_and_late_app_block(self):
        from timeweb_lifecycle import cleanup_plan
        journal = self.legacy_refusals()
        api = unittest.mock.Mock()
        api.request.side_effect = lambda path: {
            '/api/v1/apps': {'apps': copy.deepcopy(self.api.rows)},
            '/api/v1/vcs-provider/git1': {'provider': {'provider_id': 'git1'}},
            '/api/v1/floating-ips': {'ips': []}}[path]
        self.assertEqual(cleanup_plan(self.state, api)['resources'], [{'kind': 'git', 'id': 'git1'}])
        self.assertEqual(read(self.state/'timeweb-journal.json'), journal)
        self.api.rows.append({'id': 9, **self.body})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            cleanup_plan(self.state, api)
        self.api.rows.clear()
        journal['resources']['app']['retry_history'][0]['previous']['creation_attempts'] = []
        save(self.state/'timeweb-journal.json', journal)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            cleanup_plan(self.state, api)

    def test_app_fallback_price_review_and_reuse(self):
        from timeweb_app import select
        journal = self.legacy_refusals()
        self.config = journal['config']
        price = {'id': 12, 'price': 510, 'location': 'ru-3'}
        original = self.api.request
        def request(path, **kwargs):
            if path == '/api/v1/presets/apps':
                return {'backend_presets': [dict(price)]}
            return original(path, **kwargs)
        with patch('timeweb_app.API', return_value=self.api), patch.object(self.api, 'request', side_effect=request):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                self.create()
            self.assertEqual(select(self.state)['options'], [price])
            review = select(self.state, 12)
            price['price'] += 1
            with self.assertRaisesRegex(ValueError, 'changed'):
                select(self.state, 12, review['plan_sha256'])
            select(self.state, 12, select(self.state, 12)['plan_sha256'])
            body = {**self.body, 'preset_id': 12}
            price['price'] += 1
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.create(body)
            self.assertEqual(self.api.posts, 0)
            price['price'] -= 1
            self.create(body)
            self.create(body)
            self.assertEqual(self.api.posts, 1)
            self.assertEqual(read(self.state/'timeweb-journal.json')['resources']['git'], {'id': 'git1'})

    def test_review_and_success_one_post_preserves_history(self):
        before = read(self.state/'timeweb-journal.json')
        retry_create(self.state)
        self.assertEqual(before, read(self.state/'timeweb-journal.json'))
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            retry_create(self.state, 'wrong')
        self.approve()
        self.assertEqual(self.api.posts, 0)
        self.create()
        self.create()
        self.assertEqual(self.api.posts, 1)
        app = read(self.state/'timeweb-journal.json')['resources']['app']
        self.assertEqual(app['retry_history'][0]['previous'], before['resources']['app'])
        self.assertNotIn('retry_permit', app)

    def test_lost_reply_requires_new_review(self):
        self.approve()
        self.api.lost = True
        with self.assertRaises(TimewebError):
            self.create()
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.create()
        self.assertEqual(self.api.posts, 1)
        self.approve()
        self.api.lost = False
        self.create()
        self.assertEqual(self.api.posts, 2)

    def test_late_candidate_before_post_and_after_post_blocks_without_deletion(self):
        self.approve()
        self.api.rows = [{'id': 9, **self.body}]
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.create()
        self.assertEqual(self.api.posts, 0)
        self.api.rows = []
        self.api.late = True
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.create()
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(read(self.state/'timeweb-journal.json')['resources']['app']['id'], 1)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.create()
        self.assertEqual(self.api.posts, 1)

    def test_incomplete_inventory_and_stale_review(self):
        self.api.total = 1
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            retry_create(self.state)
        self.api.total = None
        plan = retry_create(self.state)
        self.api.rows = [{'id': 8, 'name': 'other'}]
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            retry_create(self.state, plan['plan_sha256'])
        self.assertEqual(self.api.posts, 0)

    def test_changed_inventory_request_or_expired_permission_never_posts(self):
        self.approve()
        with self.assertRaisesRegex(ValueError, 'request changed'):
            self.create({**self.body, 'name': 'wrong'})
        self.api.rows = [{'id': 8, 'name': 'other'}]
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            self.create()
        self.api.rows = []
        with patch('timeweb_retry.time.time', return_value=9999999999):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.create()
        self.assertEqual(self.api.posts, 0)


if __name__ == '__main__':
    unittest.main()
