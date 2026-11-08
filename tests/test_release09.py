import io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from portable_context.core import Store,key,canonical
from portable_context.workspace import Workspace
from portable_context.attribution import attribution
from portable_context.wiki import apply_pages,read_page
from portable_context.catalog import Catalog


def record(mid='1',**extra):
    return dict(source='claude-code',instance='fixture',conversation_id='chat',message_id=mid,
                role='user',text='Human request',raw={'type':'user'},**extra)


class Release09(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'memory');self.ws=Workspace(self.store)

    def ingest(self,items):
        self.store.ingest(items);return self.store.conversations()[0]['conversation']

    def commit(self,job,task,reviews=None,pages=None):
        return self.ws.commit_enrichment(job['id'],task['position'],task['lease_token'],pages or [],
                                        'Reviewed selected evidence','No additional durable claim',reviews)

    def test_mixed_first_message_existing_archives_and_citation_ranges(self):
        r=record();r.update(role='system',text='<system-reminder>Injected context</system-reminder>\nPlease preserve my request')
        self.ingest([r]);mid=key('claude-code','fixture','chat','1')
        with self.store.connect() as c: before=[tuple(x) for x in c.execute('SELECT * FROM revisions')]
        current=self.store.read(mid)
        self.assertEqual(current['role'],'user');self.assertNotIn('Injected',current['display_text'])
        self.assertEqual(self.store.search('preserve')[0]['role'],'user')
        exact=self.ws.read_revision(mid,current['revision']);self.assertEqual(json.loads(exact['json_fragment'])['role'],'system')
        evidence=self.ws.read_evidence(mid,current['revision'])
        ref={'message_key':mid,'revision':current['revision']}
        page={'id':'first','title':'First request','text':'Preserve my request','kind':'user_statement','sources':[ref]}
        with self.store.connect() as c:
            with self.assertRaises(ValueError):apply_pages(self.ws,c,[page],'test')
        page['sources']=[{**ref,'text_range':evidence['text_range']}]
        with self.store.connect() as c:apply_pages(self.ws,c,[page],'test')
        self.assertFalse(read_page(self.ws,'first')['attribution_warnings'])
        with self.store.connect() as c:self.assertEqual(before,[tuple(x) for x in c.execute('SELECT * FROM revisions')])
        for raw in ({'type':'api-request-blob'},{'type':'user','isMeta':True}):
            self.assertEqual(attribution({**r,'raw':raw})['effective_role'],'system')
        self.assertEqual(attribution({**r,'text':'<system-reminder>unclosed request'})['effective_role'],'system')
        self.assertEqual(attribution({**r,'text':'<system-reminder>x</system-reminder><task-notification>job</task-notification>'})['effective_role'],'system')

    def test_selective_ledger_resume_equivalence_changes_and_legacy_unknown(self):
        cid=self.ingest([record('1'),record('2'),{**record('3'),'role':'system','raw':{'type':'mode'},'text':'mode change'}])
        job=self.ws.start_enrichment(chunk_size=1)
        self.assertEqual(job['total'],2)
        task=self.ws.claim_enrichment(job['id'],'a')['task']
        self.commit(job,task,[{**task['sources'][0],'status':'reviewed','detail':'Read all dialogue text, not raw fields.'}])
        result=self.ws.finish_enrichment(job['id'],'Stop after the first human request')
        counts=result['coverage']['conversations'][0]
        self.assertEqual((counts['reviewed'],counts['unseen'],counts['excluded']),(1,1,1))
        self.assertFalse(counts['whole_conversation_complete'])
        new=self.ws.start_enrichment([cid]);self.assertEqual(new['total'],1)
        next_task=self.ws.claim_enrichment(new['id'],'b')['task']
        self.commit(new,next_task)  # Old API publication without review declarations.
        self.assertEqual(self.ws.review_coverage()['conversations'][0]['unseen'],1)
        self.store.ingest([{**record('1'),'capture_context':{'exported_at':'later'}}])
        self.assertEqual(self.ws.review_coverage()['conversations'][0]['reviewed'],1)
        self.store.ingest([{**record('1'),'text':'New actual user decision'}])
        self.assertEqual(self.ws.review_coverage()['conversations'][0]['changed'],1)
        self.assertEqual(self.ws.review_coverage(scope='all')['conversations'][0]['reviewed'],0)

    def test_atomic_review_invalid_reference_retry_and_active_lease(self):
        self.ingest([record()]);job=self.ws.start_enrichment();task=self.ws.claim_enrichment(job['id'],'a')['task']
        with self.assertRaises(ValueError):self.ws.finish_enrichment(job['id'],'still active')
        ref=task['sources'][0];reviews=[{**ref,'status':'partial','detail':'Read only first sentence.'}]
        bad={'id':'bad','title':'Bad','text':'x','kind':'invented','sources':[ref]}
        with self.assertRaises(ValueError):self.commit(job,task,reviews,[bad])
        self.assertEqual(self.ws.review_coverage()['conversations'][0]['partial'],0)
        with self.assertRaises(ValueError):self.commit(job,task,[{**reviews[0],'revision':'unknown'}])
        first=self.commit(job,task,reviews);second=self.commit(job,task,reviews)
        self.assertEqual(first['receipt_hash'],second['receipt_hash'])
        self.assertEqual(self.ws.review_coverage()['conversations'][0]['partial'],1)
        self.assertEqual(self.ws.describe()['unfinished_jobs'],[])

    def test_bounded_previews_and_paged_evidence(self):
        self.ingest([{**record(str(i)),'text':'x'*6000} for i in range(4)])
        job=self.ws.start_enrichment(max_chars=2000)
        self.assertEqual(job['total'],4)
        task=self.ws.claim_enrichment(job['id'],'a')['task']
        self.assertLessEqual(sum(len(x['text']) for x in task['previews']),2000)
        ref=task['sources'][0];one=self.ws.read_evidence(**ref,max_chars=1000)
        self.assertEqual(one['next_offset'],1000)
        self.assertEqual(self.ws.read_evidence(**ref,offset=1000)['text_range'],{'start':1000,'end':6000})

    def test_dialogue_omits_tool_only_rows_but_exposes_parent_evidence(self):
        self.ingest([record(),{**record('call'),'role':'assistant','text':'[Tool call: Read]','parent_id':'1',
                              'attachments':[{'kind':'tool_use','name':'call.json'}]},
                     {**record('result'),'role':'tool','text':'[Tool result: call]','parent_id':'call'},
                     {**record('answer'),'role':'assistant','text':'Here is the result.','parent_id':'result'}])
        job=self.ws.start_enrichment();task=self.ws.claim_enrichment(job['id'],'reader')['task']
        self.assertEqual(len(task['sources']),2)
        self.assertEqual(len(task['context_refs']),2)
        detail=self.ws.read_evidence(**task['sources'][-1])
        self.assertEqual(detail['current_parent_reference']['message_key'],key('claude-code','fixture','chat','result'))
        report=self.ws.review_coverage()['conversations'][0]
        self.assertEqual((report['unseen'],report['excluded']),(2,2))
        self.assertEqual(self.ws.review_coverage(scope='all')['conversations'][0]['unseen'],4)

    def test_repeated_project_assignment_does_not_create_business_changes(self):
        from portable_context.capture import capture,import_capture
        catalog=Catalog(self.store);p=catalog.create_project('stable','Stable')
        packet=self.root/'packet'
        capture([record()],packet,memory_project={'id':p['id'],'existing':True,'basis':'user_selection'})
        first=import_capture(self.store,packet,[self.root]);before=catalog.project(p['id'])
        with self.store.connect() as c:assignments=[tuple(r) for r in c.execute('SELECT * FROM catalog_assignments')]
        second=import_capture(self.store,packet,[self.root]);after=catalog.project(p['id'])
        self.assertEqual(second['messages']['unchanged'],1)
        self.assertEqual(before,after)
        with self.store.connect() as c:self.assertEqual(assignments,[tuple(r) for r in c.execute('SELECT * FROM catalog_assignments')])

    def test_archived_dialogue_text_is_paged_and_cited_from_verified_payload(self):
        from portable_context.capture import capture,import_capture
        text='<system-reminder>'+('context '*800)+'</system-reminder>'+('Human evidence. '*2500)
        packet=self.root/'long'
        capture([{**record(),'role':'system','text':text}],packet)
        import_capture(self.store,packet,[self.root])
        current=self.store.read(key('claude-code','fixture','chat','1'))
        self.assertEqual(current['effective_role'],'user')
        self.assertTrue(current['text_backing'])
        ref={'message_key':current['key'],'revision':current['revision']}
        read=self.ws.read_evidence(**ref,max_chars=1000)
        self.assertTrue(read['text_complete']);self.assertIsNotNone(read['next_offset'])
        self.assertGreater(read['total_chars'],30000)
        self.assertNotIn('context',read['text'])
        page={'id':'long','title':'Long user evidence','text':'Human evidence','kind':'user_statement',
              'sources':[{**ref,'payload_range':read['payload_range']}]}
        with self.store.connect() as c:apply_pages(self.ws,c,[page],'test')
        self.assertFalse(read_page(self.ws,'long')['attribution_warnings'])
        (self.store.root/'originals'/read['text_backing']['sha256']).write_text('corrupt')
        broken=self.ws.read_evidence(**ref)
        self.assertFalse(broken['text_complete']);self.assertEqual(broken['effective_role'],'unknown')

    def test_s3_proof_reuse_target_change_expiry_and_forced_repair(self):
        self.ingest([{**record(),'attachments':[{'name':'a.txt','path':'a.txt'}]}])
        catalog=Catalog(self.store,[self.root]);path=self.root/'a.txt';path.write_text('payload')
        with self.store.connect() as c:oid=c.execute('SELECT id FROM catalog_occurrences').fetchone()[0]
        cfg=self.root/'s3.json';config={'endpoint':'https://test.invalid','bucket':'one'};cfg.write_text(canonical(config))
        class S3:
            def __init__(self):self.data={};self.gets=0;self.puts=0
            def upload_file(self,path,bucket,key,ExtraArgs):self.data[bucket,key]=Path(path).read_bytes();self.puts+=1;self.meta=ExtraArgs['Metadata']
            def head_object(self,Bucket,Key):return {'ContentLength':len(self.data[Bucket,Key]),'Metadata':self.meta}
            def get_object(self,Bucket,Key):self.gets+=1;return {'Body':io.BytesIO(self.data[Bucket,Key]),'VersionId':'1'}
        client=S3()
        with patch.dict(os.environ,{'PC_ATTACHMENT_S3_CONFIG':str(cfg)}),patch('portable_context.backup.s3_client',return_value=client):
            first=catalog.store_attachment(oid,server_path=str(path));sha=first['sha256']
            self.assertEqual(first['s3']['status'],'verified')
            self.assertEqual(catalog.sync_blob(sha)['status'],'previously_verified');self.assertEqual(client.gets,1)
            client.data['one','attachments/'+sha]=b'corrupt'
            self.assertEqual(catalog.sync_blob(sha,force=True)['status'],'verified');self.assertEqual(client.puts,2)
            config['bucket']='two';cfg.write_text(canonical(config))
            self.assertEqual(catalog.sync_blob(sha)['status'],'verified');self.assertEqual(client.puts,3)
            with self.store.connect() as c:c.execute("UPDATE blob_verifications SET verified_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(catalog.sync_blob(sha)['status'],'verified')
            with patch('portable_context.backup.s3_client',side_effect=RuntimeError('private')):
                self.assertEqual(catalog.sync_blob(sha,force=True)['status'],'failed')
            self.assertEqual(catalog.sync_blob(sha)['status'],'verified')
