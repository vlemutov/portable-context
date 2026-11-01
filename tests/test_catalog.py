import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portable_context.core import Store, canonical, digest, key
from portable_context.catalog import Catalog, summary, project_catalog
from portable_context.capture import capture, import_capture
from portable_context.workspace import Workspace
from portable_context import backup


def event(**changes):
    return dict({'source': 'test', 'instance': 'a', 'conversation_id': 'chat', 'message_id': '1',
                 'role': 'user', 'text': 'A project document.', 'attachments': [{'path': '/client/report.txt',
                 'name': 'report.txt', 'role': 'input', 'purpose': 'requirements', 'status': 'unknown'}]}, **changes)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'memory')
        self.catalog = Catalog(self.store, [self.root / 'allowed'])
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('PC_ATTACHMENT_S3_CONFIG', None)
        self.addCleanup(self.env.stop)

    def upload(self, data=b'project requirements', oid=None):
        oid = oid or self.catalog.files()[0]['occurrences'][0]['id']
        return self.catalog.store_attachment(oid, content_base64=base64.b64encode(data).decode(),
                                             expected_sha256=hashlib.sha256(data).hexdigest())

    def test_transaction_creates_passports_project_and_rollback_preserves_consistency(self):
        self.store.ingest([event()])
        projects = self.catalog.projects()
        self.assertEqual(projects[0]['basis'], 'unclassified')
        self.assertTrue(Path(self.catalog.project(projects[0]['id'])['entry_path']).exists())
        occurrence = self.catalog.files()[0]['occurrences'][0]
        self.assertEqual(occurrence['revision'], self.store.read(occurrence['message_key'])['revision'])
        self.assertEqual(occurrence['metadata']['purpose'], 'requirements')
        self.assertEqual(summary(self.store)['missing_originals'], 1)
        with self.assertRaises(ValueError):
            self.store.ingest([event(conversation_id='rollback'), {'broken': True}])
        self.assertEqual(len(self.catalog.projects()), 1)

    def test_dedup_versions_and_equal_names_do_not_merge(self):
        self.store.ingest([event(), event(message_id='2', attachments=[{'name': 'report.txt', 'path': '/else/report.txt'}])])
        files = self.catalog.files()
        self.assertEqual(len(files), 2)
        for f in files:
            receipt = self.upload(oid=f['occurrences'][0]['id'])
            self.assertEqual(receipt['s3']['status'], 'not_configured')
        self.assertEqual(self.store.stats()['originals'], 1)
        first = files[0]['occurrences'][0]
        self.upload(oid=first['id'])
        with self.assertRaises(ValueError):
            self.upload(b'new bytes', first['id'])
        self.store.ingest([event(text='New revision')])
        item = next(f for f in self.catalog.files() if f['occurrences'][0]['metadata']['path'] == '/client/report.txt')
        self.upload(b'new requirements', item['occurrences'][0]['id'])
        self.assertEqual(len(self.catalog.file(item['id'])['versions']), 2)
        self.assertEqual(self.catalog.file(item['id'])['occurrence_count'], 2)
        self.assertEqual(summary(self.store)['missing_originals'], 0)

    def test_authorization_checksum_and_byte_read(self):
        self.store.ingest([event()])
        oid = self.catalog.files()[0]['occurrences'][0]['id']
        secret = self.root / 'outside.txt'
        secret.write_text('not authorized')
        with self.assertRaises(ValueError):
            self.catalog.store_attachment(oid, server_path=str(secret))
        with self.assertRaises(ValueError):
            self.catalog.store_attachment(oid, content_base64='%%')
        with self.assertRaises(ValueError):
            self.catalog.store_attachment(oid, content_base64='eA==', expected_sha256='0' * 64)
        result = self.upload()
        chunk = self.catalog.read_bytes(result['sha256'], offset=8, max_bytes=5)
        self.assertEqual(base64.b64decode(chunk['content_base64']), b'requi')
        (self.store.root / 'originals' / result['sha256']).write_bytes(b'corrupted')
        with self.assertRaises(ValueError):
            self.catalog.read_bytes(result['sha256'])

    def test_scoped_extraction_and_obsolete_filter(self):
        self.store.ingest([event(memory_project={'id': 'atlas', 'name': 'Atlas', 'basis': 'user_selection'})])
        result = self.upload(b'Unique requirements for Atlas')
        pid = self.catalog.projects()[0]['id']
        self.assertEqual(len(self.catalog.search('requirements', pid)), 1)
        self.assertEqual(self.catalog.search('requirements', 'other'), [])
        excerpts = self.catalog.excerpts(result['sha256'])
        self.assertEqual(excerpts[0]['locator']['char_start'], 0)
        changed = event(attachments=[{'path': '/client/report.txt', 'name': 'report.txt', 'status': 'obsolete'}])
        self.store.ingest([changed])
        oid = self.catalog.files()[0]['occurrences'][0]['id']
        self.upload(b'Unique requirements for Atlas', oid)
        self.assertEqual(self.catalog.search('requirements', pid), [])

    def test_external_image_excerpts_are_attributed_and_immutable(self):
        self.store.ingest([event(attachments=[{'name': 'screen.png'}])])
        receipt = self.upload(b'opaque image fixture')
        sha = receipt['sha256']
        self.assertEqual(self.catalog.file(self.catalog.files()[0]['id'])['versions'][0]['extraction'], 'needs_extractor')
        chunks = [{'text': 'Bucket settings', 'locator': {'image': 1, 'region': 'visible heading'}}]
        self.catalog.add_excerpts(sha, chunks, 'agent visual transcription', 'test-agent')
        self.assertTrue(self.catalog.add_excerpts(sha, chunks, 'agent visual transcription', 'test-agent')['unchanged'])
        with self.assertRaises(ValueError):
            self.catalog.add_excerpts(sha, [{'text': 'different', 'locator': {'page': 1}}], 'ocr', 'other')
        self.assertEqual(self.catalog.excerpts(sha)[0]['method'], 'agent visual transcription')

    def test_project_versions_manual_edits_and_backup_restore(self):
        self.store.ingest([event()])
        receipt = self.upload()
        pid = self.catalog.projects()[0]['id']
        page = self.catalog.project(pid)
        path = Path(page['entry_path'])
        path.write_text('Human project notes', encoding='utf-8')
        update = self.catalog.update_project(pid, 'Atlas', 'Reviewed project purpose', [], 1, 'agent')
        self.assertTrue(update['projection']['conflicts'])
        self.assertEqual(path.read_text(encoding='utf-8'), 'Human project notes')
        with self.assertRaises(ValueError):
            self.catalog.update_project(pid, 'Stale edit', '', [], 1, 'agent')
        Workspace(self.store)
        result = backup.create(self.store.root, self.root / 'backups')
        backup.restore(result['path'], self.root / 'restored')
        restored = Catalog(Store(self.root / 'restored'))
        self.assertEqual(restored.file(self.catalog.files()[0]['id'])['versions'][0]['sha256'], receipt['sha256'])
        self.assertEqual(base64.b64decode(restored.read_bytes(receipt['sha256'])['content_base64']), b'project requirements')

    def test_migration_indexes_old_revisions_without_changing_sources(self):
        self.store.ingest([event(), event(text='current')])
        with self.store.connect() as con:
            before = con.execute('SELECT record,revision FROM messages').fetchone()
            for table in ('catalog_projects', 'catalog_assignments', 'catalog_files', 'catalog_occurrences'):
                con.execute('DELETE FROM ' + table)
            con.execute("DELETE FROM metadata WHERE key='catalog_version'")
        migrated = Store(self.store.root)
        files = Catalog(migrated).files()
        self.assertEqual(Catalog(migrated).file(files[0]['id'])['occurrence_count'], 2)
        with migrated.connect() as con:
            after = con.execute('SELECT record,revision FROM messages').fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_s3_reads_back_bytes_and_failed_sync_keeps_backup_original(self):
        self.store.ingest([event()])
        config = self.root / 's3.json'
        config.write_text(json.dumps({'bucket': 'test'}))
        os.environ['PC_ATTACHMENT_S3_CONFIG'] = str(config)
        class S3:
            def __init__(self):
                self.data = None
                self.uploads = 0
            def head_object(self, **kwargs):
                if self.data is None:
                    raise FileNotFoundError()
                return {'ContentLength': len(self.data), 'Metadata': self.metadata}
            def upload_file(self, path, bucket, key, ExtraArgs):
                self.data = Path(path).read_bytes()
                self.metadata = ExtraArgs['Metadata']
                self.uploads += 1
            def get_object(self, **kwargs):
                return {'Body': io.BytesIO(self.data)}
        client = S3()
        with patch.object(backup, 's3_client', return_value=client):
            first = self.upload()
            self.assertEqual(first['s3']['status'], 'verified')
            self.upload()
            self.assertEqual(client.uploads, 1)
            client.data = b'x' * len(client.data)
            self.assertEqual(self.upload()['s3']['status'], 'verified')
            self.assertEqual(client.uploads, 2)
        with patch.object(backup, 's3_client', side_effect=RuntimeError('secret must not escape')):
            failed = self.upload()
            self.assertEqual(failed['s3']['status'], 'failed')
            self.assertNotIn('secret', canonical(failed))
            self.assertTrue((self.store.root / 'originals' / failed['sha256']).exists())

    def test_capture_freezes_bytes_and_project_before_import_and_resumes(self):
        source = self.root / 'allowed'
        source.mkdir()
        path = source / 'input.txt'
        path.write_text('Original requirements', encoding='utf-8')
        records = [event(attachments=[{'path': str(path), 'name': 'input.txt', 'purpose': 'brief'},
                                     {'path': str(source / 'gone.pdf'), 'name': 'gone.pdf'}])]
        bundle = self.root / 'bundle'
        report = capture(records, bundle, [source], {'id': 'atlas', 'name': 'Atlas', 'basis': 'user_selection'})
        self.assertEqual(report['stored_occurrences'], 1)
        self.assertEqual(report['missing_occurrences'], 1)
        self.assertTrue((bundle / 'catalog/vault/projects/INDEX.md').exists())
        path.unlink()  # The source is gone; immutable capture is sufficient.
        result = import_capture(self.store, bundle, [self.root])
        self.assertEqual(result['messages']['inserted'], 1)
        self.assertEqual(import_capture(self.store, bundle, [self.root])['messages']['unchanged'], 1)
        sha = result['attachments'][0]['sha256']
        self.assertEqual(base64.b64decode(self.catalog.read_bytes(sha)['content_base64']), b'Original requirements')
        self.assertEqual(self.catalog.projects()[0]['basis'], 'user_selection')
        self.assertEqual(summary(self.store)['missing_originals'], 1)
        with self.assertRaises(ValueError):
            capture(records, bundle, [source])

    def test_capture_never_reads_outside_roots_and_rejects_tampering(self):
        source = self.root / 'outside.txt'
        source.write_text('private')
        bundle = self.root / 'bundle'
        capture([event(attachments=[{'path': str(source)}])], bundle, [])
        records = json.loads((bundle / 'records.json').read_text(encoding='utf-8'))
        self.assertEqual(records[0]['attachments'][0]['export_observation']['reason'], 'outside_allowed_roots')
        (bundle / 'records.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            import_capture(self.store, bundle, [self.root])

    def test_annotations_preserve_source_versions_and_report_staleness(self):
        self.store.ingest([event()])
        f = self.catalog.files()[0]
        occurrence = f['occurrences'][0]
        citations = [{'message_key': occurrence['message_key'], 'revision': occurrence['revision']}]
        self.catalog.annotate(occurrence['id'], 'Project requirements', 'input', 'unknown', 'inference', citations, 'agent', 0)
        self.assertEqual(self.store.read(occurrence['message_key'])['revision'], occurrence['revision'])
        self.assertEqual(self.catalog.file(f['id'])['occurrences'][0]['annotation']['version'], 1)
        with self.assertRaises(ValueError):
            self.catalog.annotate(occurrence['id'], 'Overwriting', 'output', 'approved', 'inference', citations, 'agent', 0)
        self.store.ingest([event(text='Changed context')])
        from portable_context.wiki import lint
        self.assertIn('file_annotation_source_changed', [i['issue'] for i in lint(Workspace(self.store))['issues']])


if __name__ == '__main__':
    unittest.main()
