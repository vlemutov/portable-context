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
