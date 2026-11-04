import base64
import json
from pathlib import Path
import tempfile
import unittest

from portable_context.core import Store,canonical,key
from portable_context.workspace import Workspace
from portable_context.capture import capture,import_capture,load_package
from portable_context.catalog import Catalog
from portable_context.transfer import Uploads
from portable_context.claude_code import convert,discover
from portable_context.privacy import screen_value,screen_bytes
from portable_context.wiki import lint,cleanup_placeholders


def event(**kw):
    return dict(source='claude-code',instance='test',conversation_id='chat',message_id='one',role='user',text='Hello',**kw)


class Release05Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory')
        self.workspace=Workspace(self.store,[self.root/'uploads'])

    def test_rebuild_identical_sources_does_not_create_revisions(self):
        file=self.root/'file.txt'
        file.write_text('unchanged',encoding='utf-8')
        r=event(attachments=[{'path':str(file)}])
        a,b=self.root/'a',self.root/'b'
        capture([r],a,[self.root])
        capture([r],b,[self.root])
        self.assertEqual(load_package(a)[1],load_package(b)[1])
        import_capture(self.store,a,[self.root])
        result=import_capture(self.store,b,[self.root])
        self.assertEqual(result['messages'],{'inserted':0,'revised':0,'unchanged':1})
        self.assertEqual(self.store.stats()['revisions'],1)
        self.assertEqual(self.workspace.list_sources()[0]['format'],'claude-code')

    def test_payload_large_parts_roundtrip_and_thinking_excluded_from_search(self):
        data=[event(payloads=[{'value':{'text':'x'*10000},'kind':'tool_result','block_index':0},
                            {'value':{'text':'privateconcept'},'kind':'thinking','block_index':1,'searchable':False}]),
              dict(event(),message_id='two',text='y'*4000)]
        folder=self.root/'capture'
        report=capture(data,folder,part_bytes=1000)
        self.assertEqual(len(report['parts']),2)
        imported=import_capture(self.store,folder,[self.root])
        self.assertEqual(len(imported['attachments']),2)
        self.assertEqual(Catalog(self.store).search('privateconcept'),[])
        self.assertTrue(imported['complete'])

    def test_block_and_redact_all_raw_and_file_copies(self):
        token='sk-'+('a'*30)
        with self.assertRaisesRegex(ValueError,'screening'):
            capture([event(raw={'secret':token})],self.root/'blocked')
        file=self.root/'secret.json'
        file.write_text(json.dumps({'api_key':token}),encoding='utf-8')
        folder=self.root/'redacted'
        report=capture([event(raw={'secret':token},attachments=[{'path':str(file)}])],folder,[self.root],secret_policy='redact')
        self.assertEqual(report['quarantined_files'],1)
        self.assertNotIn(token,canonical(load_package(folder)[1]))
        self.assertFalse(list((folder/'blobs').iterdir()))
        self.assertTrue(screen_bytes(file.read_bytes())[0])

    def test_embedded_image_decoded_and_screened(self):
        token='sk-'+('b'*30)
        b={'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(token.encode()).decode()}}
        with self.assertRaisesRegex(ValueError,'screening'):
            capture([event(payloads=[{'kind':'image','value':b}])],self.root/'blocked')

    def test_nested_tool_image_is_screened_before_archiving(self):
        token='sk-'+('c'*30)
        image={'type':'image','source':{'type':'base64','data':base64.b64encode(token.encode()).decode()}}
        value={'content':[image]}
        scrubbed,findings=screen_value(value,redact=True)
        self.assertTrue(findings)
        self.assertNotIn(image['source']['data'],canonical(scrubbed))
        with self.assertRaisesRegex(ValueError,'screening'):
            capture([event(payloads=[{'kind':'tool_result','value':value}])],self.root/'blocked-nested')

    def test_corrupt_blob_is_rejected_before_messages(self):
        folder=self.root/'capture'
        report=capture([event(payloads=[{'value':{'content':'evidence'},'kind':'tool_result'}])],folder)
        (folder/report['blobs'][0]['path']).write_text('tampered')
        with self.assertRaisesRegex(ValueError,'checksum'):
            import_capture(self.store,folder,[self.root])
        self.assertEqual(self.store.stats()['messages'],0)

    def test_upload_interrupt_retry_bounds_and_finish(self):
        folder=self.root/'capture'
        manifest=capture([event()],folder)
        manifest=json.loads((folder/'manifest.json').read_text())
        uploads=Uploads(self.workspace)
        uid=uploads.begin(manifest)['upload_id']
        file=manifest['parts'][0]
        data=(folder/file['path']).read_bytes()
        half=len(data)//2
        chunk={'path':file['path'],'offset':0,'content_base64':base64.b64encode(data[:half]).decode()}
        uploads.put(uid,[chunk])
        uploads.put(uid,[chunk])
        self.assertEqual(Uploads(self.workspace).status(uid)['files'][0]['offset'],half)
        with self.assertRaises(ValueError):
            uploads.put(uid,[dict(chunk,path='../escape')])
        uploads.put(uid,[dict(chunk,offset=half,content_base64=base64.b64encode(data[half:]).decode())])
        result=uploads.finish(uid)
        self.assertEqual(result['messages']['inserted'],1)
        self.assertEqual(uploads.finish(uid)['messages']['unchanged'],1)
        self.assertEqual(len(self.workspace.receipts()),1)

    def test_projects_empty_assign_archive_and_cleanup_preview(self):
        catalog=Catalog(self.store)
        p=catalog.create_project('work','Work')
        self.assertEqual(catalog.projects()[0]['conversations'],0)
        self.assertTrue(Path(p['entry_path']).exists())
        r=event(parent_id='missing')
        self.store.ingest([r])
        r['parent_id']=None
        self.store.ingest([r])
        report=lint(self.workspace,limit=1)
        self.assertEqual(report['summary']['unused_placeholder'],1)
        self.assertNotIn('referenced_source_not_imported',report['summary'])
        preview=cleanup_placeholders(self.workspace)
        self.assertEqual(cleanup_placeholders(self.workspace,preview['preview_token'])['count'],1)
        self.assertEqual(self.store.stats()['revisions'],2)
        archive=catalog.archive_project(p['id'],p['version'])
        self.assertEqual(archive['status'],'archived')
        self.assertNotIn(p['id'],[x['id'] for x in catalog.projects()])
        self.assertIn(p['id'],[x['id'] for x in catalog.projects(include_archived=True)])

    def test_transcript_tools_partial_tail_and_stable_relocated_ids(self):
        rows=[{'type':'user','uuid':'a','sessionId':'s','message':{'content':'Question'}},
              {'type':'assistant','uuid':'b','parentUuid':'a','sessionId':'s',
               'message':{'content':[{'type':'thinking','thinking':'private'},
                    {'type':'tool_use','id':'call','name':'Read','input':{'file_path':'/chosen/a.txt'}}]}},
              {'type':'user','uuid':'c','parentUuid':'b','sessionId':'s',
               'message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'evidence'}]}}]
        a,b=self.root/'a.jsonl',self.root/'b.jsonl'
        a.write_text('\n'.join(map(canonical,rows))+'\n',encoding='utf-8')
        b.write_bytes(a.read_bytes()+b'{"incomplete"')
        records,report=convert([a,b],'test')
        self.assertEqual(len(records),3)
        self.assertEqual(records[-1]['role'],'tool')
        self.assertEqual(records[-1]['payloads'][0]['value']['content'],'evidence')
        self.assertNotIn('private',canonical(records))
        self.assertTrue(report['warnings'])
        self.assertEqual(len(discover(self.root)),1)

    def test_divergent_transcript_copy_requires_explicit_selection(self):
        a,b=self.root/'a.jsonl',self.root/'b.jsonl'
        row={'type':'user','uuid':'same','sessionId':'s','message':{'content':'first'}}
        a.write_text(canonical(row)+'\n',encoding='utf-8')
        row['message']['content']='second'
        b.write_text(canonical(row)+'\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            convert([a,b],'test')

    def test_old_capture_format_is_accepted_without_rewriting_hashes(self):
        from portable_context.core import validate,digest
        folder=self.root/'v1'
        folder.mkdir()
        data=canonical([validate(event())])
        (folder/'records.json').write_text(data,encoding='utf-8')
        (folder/'manifest.json').write_text(canonical({'format':'portable-context-capture-1','messages':1,'records_sha256':digest(data)}))
        import_capture(self.store,folder,[self.root])
        before=self.store.read(key('claude-code','test','chat','one'))
        import_capture(self.store,folder,[self.root])
        after=self.store.read(before['key'])
        self.assertEqual(before,after)

    def test_referenced_placeholder_is_never_cleaned(self):
        self.store.ingest([event(parent_id='unknown')])
        preview=cleanup_placeholders(self.workspace)
        self.assertEqual(preview['count'],0)
        self.assertEqual(lint(self.workspace)['summary']['referenced_source_not_imported'],1)

    def test_partial_attachment_delivery_resumes(self):
        records=[dict(event(),message_id=str(i),payloads=[{'kind':'tool_result','value':{'text':str(i)}}]) for i in range(3)]
        folder=self.root/'parts'
        capture(records,folder)
        first=import_capture(self.store,folder,[self.root],transfer_limit=1)
        self.assertFalse(first['complete'])
        second=import_capture(self.store,folder,[self.root],transfer_limit=2)
        self.assertTrue(second['complete'])
        self.assertEqual(self.store.stats()['revisions'],3)

    def test_same_bytes_after_touch_do_not_change_revision(self):
        import os
        f=self.root/'a.txt'
        f.write_text('same')
        r=event(attachments=[{'path':str(f)}])
        capture([r],self.root/'one',[f])
        os.utime(f,(1234567890,1234567890))
        capture([r],self.root/'two',[f])
        self.assertEqual(load_package(self.root/'one')[1],load_package(self.root/'two')[1])

    def test_local_web_archive_selection_never_packages_unselected_chat(self):
        import io
        import contextlib
        import zipfile
        from portable_context.client import main
        archive=self.root/'claude.zip'
        chats=[{'uuid':cid,'name':cid,'chat_messages':[{'uuid':'m','sender':'human','text':body}]}
               for cid,body in [('chosen','selected context'),('other','DO_NOT_UPLOAD')]]
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('conversations.json',json.dumps(chats))
        with contextlib.redirect_stdout(io.StringIO()):
            main(['prepare','--format','claude-web','--root',str(archive),'--instance','test',
                  '--session','chosen','--destination',str(self.root/'selected')])
        manifest,records=load_package(self.root/'selected')
        self.assertEqual(manifest['messages'],1)
        self.assertNotIn('DO_NOT_UPLOAD',canonical(records))

    def test_redacted_structured_credentials_are_idempotent(self):
        clean,found=screen_value({'api_key':'sk-'+('d'*30)},redact=True)
        self.assertTrue(found)
        again,found=screen_value(clean,redact=True)
        self.assertEqual(clean,again)
        self.assertFalse(found)


class ClientProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_deliver_resume_and_reject_wrong_memory(self):
        import sys
        import importlib.util
        if not importlib.util.find_spec('mcp'):
            self.skipTest('Install mcp extra')
        from portable_context.client import connect,send
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            config=root/'config.json'
            config.write_text(canonical({'mcpServers':{'portable-context':{'command':sys.executable,
                'args':['-m','portable_context','--home',str(root/'memory'),'mcp','--source-root',str(root/'uploads')]}}}))
            profile=root/'profile.json'
            await connect(config,profile)
            capture([event(payloads=[{'kind':'tool_result','value':{'text':'verified evidence'}}])],root/'package')
            first=await send(profile,root/'package')
            self.assertTrue(first['complete'])
            second=await send(profile,root/'package')
            self.assertEqual(second['messages']['unchanged'],1)
            data=json.loads(profile.read_text())
            data['workspace_id']='wrong'
            profile.write_text(canonical(data))
            with self.assertRaises(BaseException):
                await send(profile,root/'package')


if __name__=='__main__':
    unittest.main()
