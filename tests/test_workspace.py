import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from portable_context.core import Store, canonical, key
from portable_context.graph import read_graph
from portable_context.live import enqueue_batch, flush, make_server
from portable_context.workspace import Workspace
from portable_context.wiki import lint, read_page, search_pages


def event(mid='1', cid='project', **kwargs):
    return {'source': 'demo', 'instance': 'laptop', 'conversation_id': cid, 'message_id': mid,
            'role': 'user', 'text': 'Use local storage for Project Atlas.', 'title': 'Project Atlas',
            'project': {'id': 'atlas', 'name': 'Atlas'}, **kwargs}


def page(task, page_id='atlas', **kwargs):
    return {'id': page_id, 'title': 'Project Atlas', 'text': 'User prefers local storage.',
            'kind': 'user_statement', 'sources': task['sources'], 'expected_version': 0, **kwargs}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.exports = self.base / 'exports'
        self.exports.mkdir()
        self.store = Store(self.base / 'memory')
        self.ws = Workspace(self.store, [self.exports])

    def local_source(self, records):
        path = self.exports / 'events.jsonl'
        path.write_text('\n'.join(canonical(r) for r in records), encoding='utf-8')
        self.ws.register_source('local', 'Local export', 'events', 'laptop', str(path))
        return path

    def task(self, records=None):
        self.store.ingest(records or [event()])
        job = self.ws.start_enrichment()
        return job, self.ws.claim_enrichment(job['id'], 'agent-a')['task']

    def commit(self, job, task, pages, **kwargs):
        return self.ws.commit_enrichment(job['id'], task['position'], task['lease_token'], pages, 'Reviewed source', **kwargs)

    def test_selected_import_never_persists_excluded_content(self):
        secret = 'EXCLUDED-PRIVATE-CONVERSATION-8f770'
        self.local_source([event(), event(cid='other', text=secret)])
        preview = self.ws.preview('local', 'Atlas')
        selected = next(c['conversation_key'] for c in preview['conversations'] if c['conversation_id'] == 'project')
        job = self.ws.start_import('local', [selected], request_id='selection-1')
        self.ws.run_import(job['id'])
        self.assertEqual(self.store.stats()['messages'], 1)
        self.assertEqual(self.store.stats()['originals'], 0)
        for file in self.store.root.rglob('*'):
            if file.is_file():
                self.assertNotIn(secret.encode(), file.read_bytes(), str(file))

    def test_zip_selection_does_not_retain_entire_archive(self):
        path = self.exports / 'claude.zip'
        chats = [{'uuid': cid, 'name': cid, 'chat_messages': [{'uuid': '1', 'sender': 'human', 'text': content}]}
                 for cid, content in [('chosen', 'keep this'), ('other', 'SECRET-ZIP-OTHER')]]
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('conversations.json', json.dumps(chats))
            z.writestr('photos/private.txt', 'SECRET-ZIP-OTHER')
        self.ws.register_source('zip', 'Claude', 'claude', 'account', str(path))
        preview = self.ws.preview('zip')
        selected = next(c['conversation_key'] for c in preview['conversations'] if c['conversation_id'] == 'chosen')
        job = self.ws.start_import('zip', [selected])
        self.ws.run_import(job['id'])
        self.assertEqual(self.store.stats()['messages'], 1)
        self.assertFalse((self.store.root / 'originals').exists())

    def test_import_resume_snapshot_and_idempotent_request(self):
        path = self.local_source([event('1'), event('2'), event('3')])
        job = self.ws.start_import('local', request_id='stable-request')
        first = self.ws.run_import(job['id'], 1)
        self.assertEqual(first['status'], 'running')
        path.write_text(canonical(event('4')), encoding='utf-8')
        other = Workspace(Store(self.store.root))
        self.assertEqual(other.describe()['workspace_id'], self.ws.describe()['workspace_id'])
        self.assertEqual(other.start_import('local', request_id='stable-request')['id'], job['id'])
        done = other.run_import(job['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['import_counts']['inserted'], 3)
        self.assertEqual(self.store.stats()['messages'], 3)
        self.assertEqual(other.run_import(job['id'])['import_counts']['inserted'], 3)
        with self.assertRaises(ValueError):
            self.ws.start_import('local', [], request_id='stable-request')

    def test_batch_checkpoint_rolls_back_with_ingestion_failure(self):
        self.local_source([event('1'), event('2')])
        job = self.ws.start_import('local')
        original = self.store._ingest
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated interruption')
            return original(*args, **kwargs)
        with patch.object(self.store, '_ingest', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.ws.run_import(job['id'])
        self.assertEqual(self.store.stats()['messages'], 0)
        self.assertEqual(self.ws.job(job['id'])['items'], {'pending': 2})
        self.assertEqual(self.ws.run_import(job['id'])['import_counts']['inserted'], 2)

    def test_path_authorization_and_host_capability_boundary(self):
        private = self.base / 'private.jsonl'
        private.write_text(canonical(event()))
        with self.assertRaises(ValueError):
            self.ws.register_source('bad', 'Not authorized', 'events', 'laptop', str(private))
        self.ws.register_source('remote', 'Ouroboros', 'ouroboros', 'vps')
        self.assertTrue(self.ws.preview('remote')['requires_host'])
        with self.assertRaises(ValueError):
            self.ws.stage_records('remote', [event()], 'bad-instance')
        with self.assertRaises(ValueError):
            self.ws.register_source('remote', 'Changed account', 'ouroboros', 'other')

    def test_source_unavailable_status(self):
        path = self.local_source([event()])
        path.unlink()
        with self.assertRaises(ValueError):
            self.ws.preview('local')
        self.assertEqual(self.ws.source('local')['status'], 'unavailable')

    def test_structural_forks_attachments_and_revision_replacement(self):
        root = event('1', attachments=[{'path': 'diagram.png'}])
        reply = event('2', parent_id='1')
        fork = event('3', 'branch', fork_of={'conversation_id': 'project', 'message_id': '1'})
        self.local_source([root, reply, fork, event('4', 'unrelated')])
        job = self.ws.start_import('local', [key('demo', 'laptop', 'project')], include_forks=True)
        self.ws.run_import(job['id'])
        graph = read_graph(self.store)
        predicates = {e['predicate'] for e in graph['edges']}
        self.assertTrue({'forked_from', 'forked_at', 'replies_to', 'in_project', 'has_attachment'} <= predicates)
        self.assertEqual(self.store.stats()['conversations'], 2)
        self.store.ingest([dict(reply, parent_id=None)])
        self.assertNotIn('replies_to', {e['predicate'] for e in read_graph(self.store)['edges']})
        self.store.ingest([reply])  # Known old revision must not roll graph back.
        self.assertNotIn('replies_to', {e['predicate'] for e in read_graph(self.store)['edges']})

    def test_enrichment_resume_leases_and_receipts(self):
        job, task = self.task()
        self.assertIsNone(self.ws.claim_enrichment(job['id'], 'agent-b')['task'])
        with self.store.connect() as con:
            con.execute('UPDATE job_items SET lease_until=0 WHERE job_id=?', (job['id'],))
        replacement = self.ws.claim_enrichment(job['id'], 'agent-b')['task']
        with self.assertRaises(ValueError):
            self.commit(job, task, [page(task)])
        result = self.commit(job, replacement, [page(replacement)])
        replay = self.commit(job, replacement, [page(replacement)])
        self.assertEqual(result['receipt_hash'], replay['receipt_hash'])
        self.assertEqual(read_page(self.ws, 'atlas')['version'], 1)
        self.assertEqual(self.ws.describe()['conversations_needing_enrichment'], 0)
        with self.assertRaises(ValueError):
            self.commit(job, replacement, [page(replacement, text='different retry')])

    def test_concurrent_claims_are_distinct(self):
        self.store.ingest([event(str(i)) for i in range(10)])
        job = self.ws.start_enrichment(chunk_size=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            tasks = list(pool.map(lambda i: self.ws.claim_enrichment(job['id'], 'agent-' + str(i))['task'], range(10)))
        self.assertEqual(len({t['position'] for t in tasks}), 10)

    def test_atomic_pages_and_semantic_edges(self):
        job, task = self.task()
        first = page(task, 'atlas', relations=[{'type': 'related_to', 'target': 'wiki:storage'}])
        second = page(task, 'storage', kind='inference', text='Local storage is relevant to Atlas.')
        self.commit(job, task, [first, second])
        graph = read_graph(self.store, layer='semantic')
        self.assertIn('related_to', {e['predicate'] for e in graph['edges']})
        self.assertTrue(all(e['author'] == 'agent-a' for e in graph['edges']))
        self.assertEqual(len(search_pages(self.ws, 'local')['pages']), 2)
        self.ws.refresh()
        self.assertIn('wiki:atlas', canonical(read_graph(self.store)))
        self.assertIn('[[wiki/storage|', (self.store.root / 'vault/wiki/atlas.md').read_text(encoding='utf-8'))

    def test_invalid_citation_rolls_back_every_page_and_work(self):
        job, task = self.task()
        bad = page(task, 'invalid', sources=[{'message_key': 'invented', 'revision': 'invented'}])
        with self.assertRaises(ValueError):
            self.commit(job, task, [page(task), bad])
        self.assertEqual(search_pages(self.ws)['total'], 0)
        self.assertEqual(self.ws.job(job['id'])['items'], {'leased': 1})
        self.assertEqual(read_graph(self.store, layer='semantic')['edges'], [])

    def test_manual_edits_and_stale_sources_survive(self):
        job, task = self.task()
        self.commit(job, task, [page(task)])
        old = read_page(self.ws, 'atlas')
        path = self.store.root / 'vault/wiki/atlas.md'
        path.write_text(old['body'] + '\nHuman clarification.\n', encoding='utf-8')
        second = self.ws.start_enrichment([key('demo', 'laptop', 'project')])
        next_task = self.ws.claim_enrichment(second['id'], 'agent-b')['task']
        stale_update = page(next_task, expected_version=old['version'], expected_hash=old['hash'])
        with self.assertRaises(ValueError):
            self.commit(second, next_task, [stale_update])
        self.ws.refresh()
        self.assertIn('Human clarification', path.read_text(encoding='utf-8'))
        self.assertTrue(read_page(self.ws, 'atlas')['manual_edit'])
        fresh = read_page(self.ws, 'atlas')
        merged = page(next_task, text='User prefers local storage. Human clarification.',
                      expected_version=fresh['version'], expected_hash=fresh['hash'])
        self.commit(second, next_task, [merged])
        self.assertFalse(read_page(self.ws, 'atlas')['manual_edit'])
        self.store.ingest([event(text='Now prefer a hosted database.')])
        self.ws.refresh()
        original_revision = task['sources'][0]
        evidence_page = self.store.root / 'vault/revisions' / (key(original_revision['message_key'], original_revision['revision']) + '.md')
        self.assertIn('Use local storage', evidence_page.read_text(encoding='utf-8'))
        self.assertNotIn('Now prefer a hosted database', evidence_page.read_text(encoding='utf-8'))
        self.assertTrue(read_page(self.ws, 'atlas')['stale'])
        self.assertTrue(lint(self.ws)['issues'])
        self.assertEqual(self.ws.describe()['conversations_needing_enrichment'], 1)

    def test_new_messages_during_analysis_remain_pending(self):
        job, task = self.task()
        self.store.ingest([event('2', text='A later decision')])
        self.commit(job, task, [page(task)])
        self.assertEqual(self.ws.describe()['conversations_needing_enrichment'], 1)
        self.assertEqual(self.ws.start_enrichment()['total'], 1)

    def test_no_change_requires_reason(self):
        job, task = self.task()
        with self.assertRaises(ValueError):
            self.commit(job, task, [])
        self.commit(job, task, [], no_change_reason='Only a greeting; no durable knowledge.')
        self.assertEqual(self.ws.job(job['id'])['status'], 'completed')

    def test_projection_failure_is_repairable(self):
        job, task = self.task()
        with patch('portable_context.wiki.project_wiki', side_effect=OSError('disk unavailable')):
            result = self.commit(job, task, [page(task)])
        self.assertTrue(result['projection']['pending'])
        self.assertEqual(self.ws.job(job['id'])['status'], 'completed')
        self.assertEqual(read_page(self.ws, 'atlas')['version'], 1)
        self.ws.refresh()
        self.assertTrue((self.store.root / 'vault/wiki/atlas.md').exists())

    def test_legacy_database_backfill_preserves_notes_and_revisions(self):
        self.store.ingest([event()])
        mid = key('demo', 'laptop', 'project', '1')
        note = self.store.write_note('legacy', 'Keep this manual knowledge.', [mid], 'user_statement')
        self.store.ingest([event(text='Edited source')])
        # Simulate an existing 0.1 database's absence of graph metadata in this temporary store.
        with self.store.connect() as con:
            for table in ('graph_edges', 'graph_nodes', 'metadata'):
                con.execute('DROP TABLE ' + table)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.stats()['revisions'], 2)
        self.assertEqual(reopened.read_note('legacy')['body'], note['body'])
        self.assertIn('Edited source', canonical(read_graph(reopened)['nodes']))
        self.assertEqual(reopened.search('Edited')[0]['id'], mid)

    def test_continuous_delivery_uses_identical_import_job(self):
        token = 'synthetic-token-for-local-testing'
        self.ws.register_source('producer', 'Producer', 'events', 'laptop')
        server = make_server(self.store, token, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        outbox = self.base / 'outbox'
        path = Path(enqueue_batch(outbox, 'producer', [event()]))
        endpoint = f'http://127.0.0.1:{server.server_port}'
        self.assertEqual(flush(outbox, endpoint, token)['sent'], 1)
        job = self.ws.list_jobs()[0]
        self.assertEqual(self.ws.job(job['id'])['status'], 'completed')
        path.with_suffix('.sent').rename(path)
        self.assertEqual(flush(outbox, endpoint, token)['sent'], 1)
        self.assertEqual(len(self.ws.list_jobs()), 1)
        self.assertEqual(self.store.stats()['messages'], 1)
        self.assertEqual(self.ws.describe()['conversations_needing_enrichment'], 1)


if __name__ == '__main__':
    unittest.main()
