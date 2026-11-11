import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from portable_context.core import Store, key
from portable_context.workspace import Workspace
from portable_context.errors import InputError
from portable_context.import_log import events, list_imports
from portable_context.permissions import tool_scope


class CancelImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'memory'); self.ws=Workspace(self.store)
        self.ws.register_source('host','Host','events','test')
        self.records=[dict(source='test',instance='test',conversation_id='one',message_id=str(i),
                           role='user',text='original '+str(i),raw={'native':i}) for i in range(2)]

    def stage(self, request='one', records=None):
        return self.ws.stage_records('host',records or self.records,request)

    def test_cancel_ready_preserves_real_message_and_survives_restart_retry(self):
        self.store.ingest(self.records)
        before=self.store.stats()
        job=self.stage(records=[dict(self.records[0],text='x')])
        result=self.ws.cancel_import(job['id'],'Accidental test under a real ID')
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['items'],{'cancelled':1})
        self.assertEqual(self.store.stats(),before)
        self.assertNotIn(job['id'],[j['id'] for j in self.ws.describe()['unfinished_jobs']])
        self.assertEqual(list_imports(self.store)[0]['status'],'cancelled')
        ws=Workspace(Store(self.store.root))
        self.assertEqual(ws.cancel_import(job['id'],'Retry')['details'],result['details'])
        self.assertEqual(len([e for e in events(self.store,job['id']) if e['event']=='cancelled']),1)
        self.assertEqual(ws.stage_records('host',[dict(self.records[0],text='x')],'one')['status'],'cancelled')
        with patch('portable_context.restic_backup.protection') as backup:
            with self.assertRaisesRegex(InputError,'cancelled'): ws.run_import(job['id'])
            backup.assert_not_called()
        self.assertEqual(self.store.stats(),before)

    def test_cancel_partial_retains_committed_counts_and_rejects_completed_and_other_jobs(self):
        job=self.stage(); self.ws.run_import(job['id'],1)
        result=self.ws.cancel_import(job['id'],'Stop remaining selection')
        self.assertEqual(result['items'],{'completed':1,'cancelled':1})
        self.assertEqual(result['import_counts'],dict(inserted=1,revised=0,unchanged=0))
        self.assertEqual(self.store.stats()['messages'],1)
        done=self.stage('two'); self.ws.run_import(done['id'])
        for jid in (done['id'],'unknown',self.ws.start_enrichment()['id']):
            with self.assertRaises(InputError): self.ws.cancel_import(jid,'Wrong target')
        with self.assertRaises(InputError): self.ws.cancel_import(job['id'],'')

    def test_cancellation_is_atomic_on_audit_failure(self):
        job=self.stage()
        with patch.object(self.ws,'activity',side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError): self.ws.cancel_import(job['id'],'Stop')
        self.assertEqual(self.ws.job(job['id'])['status'],'ready')
        self.assertEqual(self.ws.job(job['id'])['items'],{'pending':2})
        self.assertEqual(list_imports(self.store)[0]['status'],'in_progress')
        self.assertFalse(any(e['event']=='cancelled' for e in events(self.store,job['id'])))

    def test_cancel_between_run_preflight_and_transaction_prevents_ingestion(self):
        job=self.stage(); entered=threading.Event(); proceed=threading.Event()
        def protection(*args):
            entered.set(); self.assertTrue(proceed.wait(10))
        with patch('portable_context.restic_backup.protection',side_effect=protection):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(self.ws.run_import,job['id'])
                try:
                    self.assertTrue(entered.wait(10))
                    self.ws.cancel_import(job['id'],'Cancelled before write')
                finally: proceed.set()
                with self.assertRaisesRegex(InputError,'cancelled'): future.result(timeout=10)
        self.assertEqual(self.store.stats()['messages'],0)

    def test_running_batch_commits_before_cancel_then_pending_stays_cancelled(self):
        job=self.stage(); entered=threading.Event(); proceed=threading.Event(); cancelling=threading.Event()
        original=self.store._ingest
        def ingest(*args):
            entered.set(); self.assertTrue(proceed.wait(10)); return original(*args)
        def cancel():
            cancelling.set(); return self.ws.cancel_import(job['id'],'Stop after active batch')
        with patch.object(self.store,'_ingest',side_effect=ingest):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                running=pool.submit(self.ws.run_import,job['id'],1)
                try:
                    self.assertTrue(entered.wait(10)); cancelled=pool.submit(cancel)
                    self.assertTrue(cancelling.wait(10))
                finally: proceed.set()
                running.result(timeout=10); result=cancelled.result(timeout=10)
        self.assertEqual(result['items'],{'completed':1,'cancelled':1})
        with self.assertRaises(InputError): self.ws.run_import(job['id'])
        self.assertEqual(self.store.stats()['messages'],1)

    def test_cancel_scope_matches_job_origin(self):
        host=self.stage()
        self.assertEqual(tool_scope('cancel_import',{'job_id':host['id']},self.store),'memory:write')
        self.assertEqual(tool_scope('cancel_import',{'job_id':'unknown'},self.store),'memory:admin')
        source=Path(self.temp.name)/'source.jsonl'; source.write_text(json.dumps(self.records[0]))
        self.ws.register_source('local','Local','events','test',str(source),trusted_local=True)
        local=self.ws.start_import('local')
        self.assertEqual(tool_scope('cancel_import',{'job_id':local['id']},self.store),'memory:admin')

    def test_invalid_executor_rolls_back_and_raw_is_preserved_after_fix(self):
        with self.assertRaisesRegex(InputError,'executor'):
            self.ws.stage_records('host',self.records,'probe',{'client':'Claude','transport':'https','purpose':'test'})
        self.assertEqual(self.ws.list_jobs(),[])
        job=self.ws.stage_records('host',self.records,'probe',{'client':'Claude','agent':'test'})
        self.ws.run_import(job['id'])
        record=self.records[0]
        self.assertEqual(self.store.read(key(record['source'],record['instance'],record['conversation_id'],record['message_id']))['raw'],record['raw'])
