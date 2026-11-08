import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from portable_context.core import Store,canonical,digest,key,validate
from portable_context.capture import capture,import_capture
from portable_context.catalog import Catalog
from portable_context.workspace import Workspace
from portable_context.identity import preview,apply,fingerprint
from portable_context.graph import evidence_stale
from portable_context.claude_code import convert
from portable_context.maintenance import cleanup_preview


def event(**extra):
    return dict(source='test',instance='fixture',conversation_id='c',message_id='m',role='user',text='Original',**extra)


class Release08(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.store=Store(self.root/'memory'); Workspace(self.store,[self.root/'imports'])

    def test_metadata_equivalence_preserves_exact_citations_and_late_replay(self):
        r=event(); self.store.ingest([r]); mid=key('test','fixture','c','m')
        first=self.store.read(mid)['revision']
        altered=dict(r,capture_context={'cwd':'elsewhere'},memory_project={'id':'p','name':'Project','basis':'user_selection'})
        self.assertEqual(self.store.ingest([altered])['unchanged'],1)
        with self.store.connect() as con:
            self.assertFalse(evidence_stale(con,[{'message_key':mid,'revision':first}]))
            self.assertEqual(con.execute('SELECT count(*) FROM revisions').fetchone()[0],2)
        changed=dict(r,text='Real edit')
        self.assertEqual(self.store.ingest([changed])['revised'],1)
        self.assertEqual(self.store.ingest([dict(altered,capture_context={'cwd':'third'})])['unchanged'],1)
        self.assertEqual(self.store.read(mid)['text'],'Real edit')
        with self.store.connect() as con:
            self.assertTrue(evidence_stale(con,[{'message_key':mid,'revision':first}]))

    def test_migration_additive_and_stale_plan_refused(self):
        self.store.ingest([event()]); plan=preview(self.store)
        self.store.ingest([dict(event(),text='changed')])
        with self.assertRaisesRegex(ValueError,'changed'): apply(self.store,plan['confirmation'])
        before=self.store.stats(); plan=preview(self.store)
        self.assertTrue(apply(self.store,plan['confirmation'])['applied'])
        self.assertEqual(self.store.stats(),before)

    def test_inline_external_raw_equivalence_and_partial_distinction(self):
        r=validate(event(raw={'note':'x'*40000}))
        a={'file_id':'payload:m:raw','kind':'full_raw','sha256':digest(canonical(r['raw'])),
           'export_observation':{'method':'embedded_source_payload'}}
        external=dict(r,raw={'archived_payload':'payload:m:raw'},attachments=[a],completeness={'raw_archived':True})
        self.assertNotEqual(fingerprint(r),fingerprint(external))
        (self.store.root/'originals').mkdir(exist_ok=True)
        (self.store.root/'originals'/a['sha256']).write_text(canonical(r['raw']),encoding='utf-8')
        from portable_context.identity import verified_payloads
        self.assertEqual(fingerprint(r),fingerprint(external,verified_payloads(self.store.root/'originals')))
        self.assertNotEqual(fingerprint(r),fingerprint(dict(r,completeness={'redacted':True})))
        self.assertNotEqual(fingerprint(r),fingerprint(dict(r,raw={'unknown':'changed'})))

    def test_resume_ingests_once_totals_and_shared_blobs(self):
        rows=[dict(event(),message_id=str(i),payloads=[{'kind':'tool_result','value':{'text':'shared' if i<2 else 'other'}}]) for i in range(3)]
        packet=self.root/'packet'; capture(rows,packet)
        with patch.object(self.store,'_ingest',wraps=self.store._ingest) as ingest:
            first=import_capture(self.store,packet,[self.root],transfer_limit=1,run_id='run')
            self.assertFalse(first['complete']); self.assertEqual(first['messages']['inserted'],3)
            second=import_capture(self.store,packet,[self.root],transfer_limit=1,run_id='run')
            self.assertTrue(second['complete']); self.assertEqual(second['messages']['inserted'],3)
            self.assertEqual(ingest.call_count,1)
            self.assertEqual(second['work']['unique_blob_attempts'],2)
            self.assertEqual(len(second['attachments']),3)
        replay=import_capture(self.store,packet,[self.root],run_id='replay')
        self.assertEqual(replay['messages'],dict(inserted=0,revised=0,unchanged=3))

    def test_existing_project_offline_capture_no_placeholder(self):
        project=Catalog(self.store).create_project('existing','Known','', 'owner')
        packet=self.root/'packet'
        capture([event()],packet,memory_project={'id':project['id'],'basis':'user_selection','existing':True})
        draft=Store(packet/'catalog')
        with draft.connect() as con: self.assertEqual(con.execute('SELECT count(*) FROM catalog_projects').fetchone()[0],0)
        result=import_capture(self.store,packet,[self.root])
        self.assertEqual(result['project_id'],project['id'])

    def test_unknown_existing_project_rejected_before_upload(self):
        from portable_context.transfer import Uploads
        packet=self.root/'packet'
        prepared=capture([event()],packet,memory_project={'id':'missing','basis':'user_selection','existing':True})
        manifest=json.loads((packet/'manifest.json').read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError,'Unknown existing project'):
            Uploads(Workspace(self.store,[self.root/'imports'])).begin(manifest)

    def test_resume_uses_committed_phase_and_rechecks_policy(self):
        from portable_context.policy import set_policy
        packet=self.root/'packet'
        capture([dict(event(),payloads=[{'kind':'tool_result','value':str(i),'block_index':i} for i in range(2)])],packet)
        import_capture(self.store,packet,[self.root],transfer_limit=1,run_id='phase')
        set_policy(self.store,'no_tool_payloads','owner')
        with patch('portable_context.capture.load_package',side_effect=AssertionError('Must resume committed phase')):
            with self.assertRaisesRegex(ValueError,'policy'):
                import_capture(self.store,packet,[self.root],transfer_limit=1,run_id='phase')

    def convert_rows(self,rows,**kwargs):
        p=self.root/'session.jsonl'; p.write_text('\n'.join(canonical(r) for r in rows),encoding='utf-8')
        return convert([p],'fixture',**kwargs)

    def test_diagnostic_classified_before_nested_tools_and_full_mode_filters_thinking(self):
        row={'sessionId':'s','type':'api-request-blob','message':{'role':'user','content':[
            {'type':'text','text':'do not surface'}, {'type':'tool_use','name':'Read','input':{'file_path':'private.txt'}},
            {'type':'thinking','thinking':'private reasoning'}]}}
        records,report=self.convert_rows([row]); r=records[0]
        self.assertEqual(r['text'],''); self.assertEqual(r['attachments'],[])
        self.assertNotIn('do not surface',canonical(r)); self.assertNotIn('private reasoning',canonical(r))
        full,_=self.convert_rows([row],diagnostics='full')
        self.assertIn('do not surface',canonical(full)); self.assertNotIn('private reasoning',canonical(full))
        self.assertEqual(full[0]['attachments'],[])
        self.assertFalse(report['warnings'])

    def test_notification_and_rendered_user_are_not_human_and_excerpt_not_original(self):
        rows=[{'sessionId':'s','uuid':str(i),'type':'attachment','renderedRole':'user','attachment':a} for i,a in enumerate([
            {'type':'queued_command','commandMode':'task-notification','prompt':'Task finished'},
            {'type':'instructions','files':[{'path':'memory.md','type':'AutoMem','content':'Historical'}]},
            {'type':'file','filename':'doc.txt','content':{'type':'text','file':{'filePath':'doc.txt','content':'fragment',
                'startLine':20,'numLines':1,'totalLines':40}}}])]
        records,report=self.convert_rows(rows)
        self.assertTrue(all(r['role']=='system' for r in records)); self.assertFalse(report['warnings'])
        self.assertEqual(records[0]['raw']['source_classification']['kind'],'task_notification')
        self.assertFalse(records[2]['payloads'][0]['value']['original_bytes'])
        self.assertEqual(records[2]['payloads'][0]['value']['locator']['startLine'],20)

    def test_cleanup_keeps_import_until_exact_recovery_coverage(self):
        packet=self.root/'imports/legacy-capture'; capture([event()],packet)
        import_capture(self.store,packet,[self.root])
        plan=cleanup_preview(self.root)
        self.assertEqual(plan['items'],[])
        self.assertEqual(plan['kept'][0]['reason'],'no_verified_snapshot_covering_contents')
        from portable_context.recovery_coverage import inventory
        with self.store.connect() as con: covered=inventory(con)
        with patch('portable_context.recovery_coverage.verified',return_value=[dict(covered,snapshot_id='a'*64)]):
            plan=cleanup_preview(self.root)
        self.assertEqual(plan['items'][0]['snapshot_id'],'a'*64)


if __name__=='__main__': unittest.main()
