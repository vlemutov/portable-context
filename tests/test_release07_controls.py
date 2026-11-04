import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from portable_context.core import Store,canonical
from portable_context.workspace import Workspace
from portable_context.catalog import Catalog
from portable_context.capture import capture,import_capture
from portable_context.indexing import set_policy
from portable_context.policy import runtime_exclusion,Policy
from portable_context.claude_code import discover,convert
from portable_context.storage import report,forecast
from portable_context.maintenance import cleanup_preview,cleanup,s3_cleanup_preview,s3_cleanup

def record(**kw):
    return dict(dict(source='test',instance='synthetic',conversation_id='c',message_id='1',role='user',text='short'),**kw)

class Controls07(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory'); self.ws=Workspace(self.store,[self.root/'imports']); self.cat=Catalog(self.store)

    def test_service_index_rebuild_preserves_bytes_and_source_history(self):
        packet=self.root/'packet'
        capture([record(payloads=[{'kind':'service_event','block_index':0,'value':'uniqueneedle '*100,'searchable':False},
            {'kind':'tool_result','block_index':1,'value':'toolneedle'}])],packet,[self.root])
        import_capture(self.store,packet,[self.root]); before=self.store.stats()
        self.assertFalse(self.cat.search('uniqueneedle',include_payloads=True))
        self.assertTrue(self.cat.search('toolneedle',include_payloads=True))
        with self.store.connect() as con: originals=list(con.execute('SELECT hash,size FROM imports'))
        set_policy(self.store,'service_event',True,0,'owner')
        self.assertTrue(self.cat.search('uniqueneedle',include_payloads=True))
        set_policy(self.store,'service_event',False,1,'owner')
        self.assertFalse(self.cat.search('uniqueneedle',include_payloads=True))
        with self.assertRaisesRegex(ValueError,'conflict'): set_policy(self.store,'service_event',True,0,'owner')
        self.assertEqual(self.store.stats()['revisions'],before['revisions'])
        for sha,size in originals: self.assertEqual((self.store.root/'originals'/sha).stat().st_size,size)
        # A legacy index is trimmed once during migration, without deleting extracted chunks.
        with self.store.connect() as con:
            con.execute('DROP TABLE index_policy')
            con.execute('INSERT INTO catalog_chunks_fts SELECT sha256,position,text FROM catalog_chunks')
        Store(self.store.root)
        self.assertFalse(self.cat.search('uniqueneedle',include_payloads=True))

    def test_useful_temp_scripts_and_memory_are_discovered_without_execution(self):
        project=self.root/'.claude/projects/encoded'; project.mkdir(parents=True)
        memory=project/'memory'; memory.mkdir(); (memory/'MEMORY.md').write_text('project notes')
        script=self.root/'Temp/claude/calculation.py'; script.parent.mkdir(parents=True); script.write_text('print(2)')
        session=project/'session.jsonl'
        session.write_text(canonical({'sessionId':'s','uuid':'m','type':'assistant','cwd':str(self.root),
            'message':{'content':[{'type':'tool_use','name':'Bash','input':{'command':'python "'+str(script)+'"'}}]}}))
        self.assertEqual(discover(project)[0]['memory_roots'],[str(memory.resolve())])
        records,_=convert([session],'fixture')
        a=records[0]['attachments'][0]; self.assertIsNone(runtime_exclusion(a['path'],a))
        self.assertIsNotNone(runtime_exclusion(str(project/'tool-results/dump.txt'),a))
        self.assertIsNotNone(runtime_exclusion(str(project/'tasks/1.output'),a))
        package=self.root/'capture'; capture(records,package,[self.root])
        manifest=json.loads((package/'manifest.json').read_text())
        self.assertTrue(any((package/p['path']).read_bytes()==b'print(2)' for p in manifest['blobs']))
        denied=self.root/'denied'; capture(records,denied,[self.root],policy=Policy({'version':1,'default':'exclude','rules':[]}))
        self.assertEqual(json.loads((denied/'manifest.json').read_text())['blobs'],[])

    def test_disabled_kind_cannot_borrow_index_from_identical_other_project_blob(self):
        first=self.cat.create_project('service','Service'); second=self.cat.create_project('tool','Tool')
        for i,(project,kind) in enumerate(((first,'service_event'),(second,'tool_result'))):
            packet=self.root/str(i)
            capture([record(conversation_id=str(i),payloads=[{'kind':kind,'block_index':0,'value':'sharedneedle'}])],packet,[self.root],
                memory_project={'id':project['id'],'name':project['title'],'basis':'user_selection'})
            import_capture(self.store,packet,[self.root],project_id=project['id'])
        self.assertFalse(self.cat.search('sharedneedle',first['id'],include_payloads=True))
        self.assertTrue(self.cat.search('sharedneedle',second['id'],include_payloads=True))

    def test_synthetic_storage_benchmark_default_is_smaller_than_service_index(self):
        records=[record(message_id=str(i),payloads=[{'kind':'service_event','block_index':0,
            'value':('synthetic progress observation '+str(i)+' ')*150}]) for i in range(40)]
        packet=self.root/'benchmark'; capture(records,packet,[self.root])
        import_capture(self.store,packet,[self.root])
        other=Store(self.root/'indexed'); Workspace(other); set_policy(other,'service_event',True,0,'benchmark')
        import_capture(other,packet,[self.root])
        light=report(self.store); full=report(other)
        light_bytes=light['sqlite_bytes']+light['originals_bytes']+light['vault_bytes']
        full_bytes=full['sqlite_bytes']+full['originals_bytes']+full['vault_bytes']
        self.assertLess(light_bytes,full_bytes)
        self.assertEqual(self.store.stats()['messages'],other.stats()['messages'])
        print(canonical({'synthetic_storage_benchmark':{'source_bytes':len(canonical(records).encode()),
            'default_memory_bytes':light_bytes,'service_index_enabled_memory_bytes':full_bytes}}))

    def test_cleanup_protects_current_previous_and_requires_exact_preview(self):
        releases=self.root/'releases'; releases.mkdir()
        for i in range(3):
            p=releases/str(i); p.mkdir(); (p/'code').write_text('test')
            import os
            os.utime(p,(100+i,100+i))
        plan=cleanup_preview(self.root)
        self.assertEqual([Path(x['path']).name for x in plan['items']],['0'])
        with self.assertRaisesRegex(ValueError,'preview'): cleanup(self.root,'wrong')
        cleanup(self.root,plan['confirmation'])
        self.assertFalse((releases/'0').exists()); self.assertTrue((releases/'2').exists())
        storage=report(self.store); self.assertGreater(storage['sqlite_bytes'],0)
        self.assertTrue(forecast(self.store,1024)['fits_with_20_percent_reserve'])
        with self.assertRaises(ValueError): forecast(self.store,-1)

    def test_s3_cleanup_lists_exact_versions_and_never_attachments(self):
        from unittest.mock import MagicMock
        config=self.root/'backup-config.json'; config.write_text(canonical({'bucket':'test'}))
        manager=MagicMock(); manager.state=self.root/'state'
        client=MagicMock(); client.get_bucket_versioning.return_value={'Status':'Enabled'}
        client.get_paginator.return_value.paginate.return_value=[{'Versions':[
            {'Key':'backups/2026010'+str(i)+'.zip','VersionId':'v'+str(i),'Size':100,'ETag':'e'} for i in range(1,5)]}]
        with patch('portable_context.maintenance.configured',return_value=manager),patch('portable_context.maintenance.restic_verified',return_value=True),patch('portable_context.backup.s3_client',return_value=client):
            plan=s3_cleanup_preview(self.root); self.assertEqual(plan['bytes'],200)
            with self.assertRaisesRegex(ValueError,'preview'): s3_cleanup(self.root,'bad')
            client.delete_object.assert_not_called()
            result=s3_cleanup(self.root,plan['confirmation']); self.assertEqual(len(result['removed']),2)
            self.assertEqual({c.kwargs['VersionId'] for c in client.delete_object.call_args_list},{'v1','v2'})

    def test_concurrent_identical_original_publication_and_corruption(self):
        from concurrent.futures import ThreadPoolExecutor
        source=self.root/'document.txt'; source.write_text('shared immutable document')
        with ThreadPoolExecutor(max_workers=8) as pool:
            hashes=list(pool.map(lambda _:self.store.archive(source),range(64)))
        self.assertEqual(len(set(hashes)),1)
        with self.store.connect() as con: self.assertEqual(con.execute('SELECT count(*) FROM imports').fetchone()[0],1)
        (self.store.root/'originals'/hashes[0]).write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'checksum'): self.store.archive(source)

if __name__=='__main__': unittest.main()
