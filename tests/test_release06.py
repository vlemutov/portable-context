import asyncio
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch,AsyncMock

from portable_context.core import Store,canonical,key,digest
from portable_context.workspace import Workspace
from portable_context.catalog import Catalog,summary,project_catalog
from portable_context.capture import capture,load_package,import_capture
from portable_context.policy import Policy,set_policy,get_policy,agent_workspace
from portable_context.transfer import Uploads
from portable_context.import_log import list_imports,events
from portable_context.claude_code import convert,discover
from portable_context.control import list_sessions


def event(**kw):
    return dict(dict(source='claude-code',instance='machine',conversation_id='s',message_id='m',role='user',text='hello'),**kw)


class Release06Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory')
        self.ws=Workspace(self.store,[self.root/'uploads'])
        self.cat=Catalog(self.store)

    def packet(self,records=None,**kw):
        path=self.root/('package-'+str(len(list(self.root.glob('package-*')))))
        capture(records or [event()],path,[self.root],**kw)
        return path,json.loads((path/'manifest.json').read_text(encoding='utf-8'))

    def upload(self,path,manifest,**kw):
        up=Uploads(self.ws)
        begun=up.begin(manifest,**kw)
        for f in manifest['parts']+manifest['blobs']:
            up.put(begun['upload_id'],[{'path':f['path'],'offset':0,
                'content_base64':base64.b64encode((path/f['path']).read_bytes()).decode()}])
        return up,begun

    def test_documents_default_payloads_explicit_and_counts_deduplicated(self):
        docs=[{'file_id':'doc'+str(i),'name':str(i)+'.txt'} for i in range(3)]
        records=[event(attachments=docs,payloads=[{'kind':'tool_result','block_index':i,'value':'shared needle'} for i in range(500)])]
        path,m=self.packet(records)
        import_capture(self.store,path,[self.root])
        self.assertEqual(len(self.cat.files()),3)
        self.assertEqual(len(self.cat.files(include_payloads=True,limit=100)),100)
        self.assertEqual(self.cat.search('needle'),[])
        self.assertTrue(self.cat.search('needle',include_payloads=True))
        counts=summary(self.store)
        self.assertEqual(counts['categories']['source_payload']['current_files'],500)
        self.assertEqual(counts['categories']['document']['current_files'],3)
        self.assertEqual(counts['categories']['source_payload']['current_unique_bytes'],len(canonical('shared needle').encode()))
        self.assertEqual(len(list((self.store.root/'vault/files').glob('*.md'))),3)

    def test_legacy_category_migration_preserves_history_and_edits(self):
        r=event(attachments=[{'file_id':'payload:m:0','name':'tool_result.json','kind':'tool_result'}])
        self.store.ingest([r])
        fid=self.cat.files(include_payloads=True)[0]['id']
        with self.store.connect() as con:
            before=[tuple(r) for r in con.execute('SELECT id,hash,record FROM revisions')]
            self.store._project_file(con,'files/'+fid+'.md','old generated',{'written':0,'conflicts':[]})
            con.execute('ALTER TABLE catalog_occurrences DROP COLUMN category')
            con.execute('ALTER TABLE catalog_projects DROP COLUMN group_id')
            con.execute("DELETE FROM metadata WHERE key='control_version'")
        migrated=Store(self.store.root)
        Store(self.store.root)
        self.assertEqual(len(list((self.root/'memory-migration-backups').glob('*.zip'))),1)
        with migrated.connect() as con:
            self.assertEqual(before,[tuple(r) for r in con.execute('SELECT id,hash,record FROM revisions')])
        project_catalog(migrated)
        self.assertFalse((self.store.root/'vault/files'/f'{fid}.md').exists())
        with migrated.connect() as con:
            self.store._project_file(con,'files/'+fid+'.md','old generated',{'written':0,'conflicts':[]})
        (self.store.root/'vault/files'/f'{fid}.md').write_text('manual edit',encoding='utf-8')
        self.assertIn('files/'+fid+'.md',project_catalog(migrated)['conflicts'])

    def test_current_and_historical_counts_are_distinct(self):
        self.store.ingest([event(attachments=[{'name':'old.txt'}])])
        self.store.ingest([event(text='new',attachments=[])])
        counts=summary(self.store)
        self.assertEqual(counts['files'],0)
        self.assertEqual(counts['all_historical_files'],1)
        self.assertEqual(len(self.cat.files(include_history=True)),1)

    def test_groups_stable_rename_and_project_versioning(self):
        a=self.cat.save_group('personal','Personal','user')
        b=self.cat.save_group('work','Work','user')
        for i in range(3):
            self.cat.create_project(str(i),str(i),group_id=a['id'])
        p=self.cat.create_project('work','Work project',group_id=b['id'])
        self.assertEqual(len(self.cat.projects(group_id=a['id'])),3)
        renamed=self.cat.save_group('personal','My personal projects','user',a['version'])
        self.assertEqual(a['id'],renamed['id'])
        self.assertIn('My personal projects',(self.store.root/'vault/groups'/f'{a["id"]}.md').read_text())
        self.cat.update_project(p['id'],p['title'],'',[],p['version'],'user',group_id=a['id'])
        self.assertEqual(len(self.cat.projects(group_id=a['id'])),4)
        with self.assertRaisesRegex(ValueError,'version'):
            self.cat.update_project(p['id'],p['title'],'',[],p['version'],'user')

    def test_runtime_paths_are_cross_platform_but_clipboard_temp_is_allowed(self):
        for p in ('C:/Users/me/AppData/Local/Temp/claude/task.log','/tmp/claude/task.log',
                  '/private/var/folders/a/b/T/claude-123/task.log','/home/me/.claude/projects/p/s/tool-results/t.txt',
                  '/Users/me/.codex/sessions/s.jsonl','C:/Users/me/.claude/projects/p/s.jsonl'):
            self.assertIsNotNone(agent_workspace(p),p)
        self.assertIsNone(agent_workspace('C:/Users/me/AppData/Local/Temp/codex-clipboard.png'))
        self.assertIsNone(agent_workspace('/tmp/report.txt'))

    def test_runtime_exclusion_is_explained_and_exact_override_keeps_root_boundary(self):
        p=self.root/'.claude/projects/p/s/tool-results/output.txt'
        p.parent.mkdir(parents=True)
        p.write_text('private output')
        doc=self.root/'report.txt'
        doc.write_text('chosen document')
        r=event(attachments=[{'path':str(p)},{'path':str(doc)}])
        a,m=self.packet([r])
        self.assertEqual(m['stored_occurrences'],1)
        self.assertEqual(m['excluded_occurrences'],1)
        self.assertEqual(m['missing_occurrences'],0)
        records=load_package(a)[1]
        self.assertEqual(records[0]['attachments'][0]['export_observation']['reason'],'excluded_agent_workspace')
        b,n=self.packet([r],include_paths=[p])
        self.assertEqual(n['stored_occurrences'],2)

    def test_policy_excludes_before_screening_and_intersects_matching_rules(self):
        policy=Policy({'version':1,'default':'exclude','rules':[
            {'source':'claude-code','root':str(self.root),'permissions':'full'},
            {'root':str(self.root/'work'),'permissions':'exclude'}]})
        path,m=self.packet([event(raw={'cwd':str(self.root/'work'),'api_key':'sk-'+'z'*40}),
                            event(message_id='allowed',raw={'cwd':str(self.root)})],policy=policy)
        self.assertEqual(m['messages'],1)
        self.assertEqual(m['excluded_records'],1)
        self.assertEqual(load_package(path)[1][0]['message_id'],'allowed')

    def test_no_payload_policy_removes_duplicate_raw_and_preserves_conversation(self):
        policy=Policy({'version':1,'default':'conversation_and_references','rules':[]})
        doc=self.root/'report.txt'; doc.write_text('private bytes')
        r=event(text='The result is discussed here',raw={'api_key':'sk-'+'q'*40},attachments=[{'path':str(doc)}],
                payloads=[{'kind':'tool_result','value':{'api_key':'sk-'+'q'*40}}])
        path,m=self.packet([r],policy=policy)
        record=load_package(path)[1][0]
        self.assertEqual(record['raw'],{})
        self.assertEqual(record['text'],r['text'])
        self.assertEqual(m['blobs'],[])
        self.assertEqual(m['excluded_occurrences'],2)
        self.assertNotIn('sk-',canonical(record))
        set_policy(self.store,'conversation_and_references','user')
        up,begun=self.upload(path,m)
        self.assertTrue(up.finish(begun['upload_id'],run_id=begun['run_id'])['complete'])
        oid=self.cat.files()[0]['occurrences'][0]['id']
        with self.assertRaisesRegex(ValueError,'policy'):
            self.cat.store_attachment(oid,content_base64=base64.b64encode(b'private').decode())

    def test_server_rejects_before_upload_and_rechecks_after_policy_change(self):
        path,m=self.packet()
        set_policy(self.store,'exclude','user')
        up=Uploads(self.ws)
        with self.assertRaisesRegex(ValueError,'policy'):
            up.begin(m)
        self.assertFalse((up.root/digest(canonical(m))).exists())
        set_policy(self.store,'full','user',1)
        begun=up.begin(m)
        set_policy(self.store,'exclude','user',2)
        part=m['parts'][0]
        with self.assertRaisesRegex(ValueError,'policy'):
            up.put(begun['upload_id'],[{'path':part['path'],'offset':0,'content_base64':base64.b64encode((path/part['path']).read_bytes()).decode()}])
        self.assertFalse((up.folder(begun['upload_id'])/part['path']).exists())

    def test_restricted_project_cannot_be_bypassed_through_staging_or_legacy_archive(self):
        self.store.ingest([event()])
        pid=self.cat.projects()[0]['id']
        set_policy(self.store,'exclude','user',project_id=pid)
        self.ws.register_source('host','Host','claude-code','machine')
        with self.assertRaisesRegex(ValueError,'policy'):
            self.ws.stage_records('host',[event(message_id='next')],'batch')
        with self.assertRaisesRegex(ValueError,'policy'):
            self.store.ingest([event(message_id='next')])
        from portable_context.importers import import_path
        file=self.root/'data.jsonl'; file.write_text(canonical(event()))
        with self.assertRaisesRegex(ValueError,'Restricted'):
            import_path(self.store,file,'events','machine')
        self.assertEqual(self.store.stats()['messages'],1)
        self.assertFalse((self.store.root/'originals').exists())

    def test_send_rechecks_current_local_policy_before_connection(self):
        from portable_context.client import send
        path,m=self.packet()
        profile=self.root/'profile.json'
        profile.write_text(canonical({'workspace_id':'w','connection':{}}))
        with patch('portable_context.client.with_session',new_callable=AsyncMock) as connection:
            with self.assertRaisesRegex(ValueError,'excludes'):
                asyncio.run(send(profile,path,policy=Policy({'version':1,'default':'exclude','rules':[]})))
            connection.assert_not_called()

    def test_import_runs_separate_replays_and_do_not_count_transfer_chunks(self):
        path,m=self.packet([event(message_id=str(i),payloads=[{'kind':'tool_result','value':str(i)}]) for i in range(3)])
        up,begun=self.upload(path,m,executor={'client':'test','version':'1','agent':'Agent A'})
        first=import_capture(self.store,up.folder(begun['upload_id']),self.ws.allowed_roots,transfer_limit=1,run_id=begun['run_id'])
        self.assertFalse(first['complete'])
        up.finish(begun['upload_id'],run_id=begun['run_id'])
        again=up.begin(m,executor={'client':'test','version':'2','agent':'Agent B'})
        up.finish(again['upload_id'],run_id=again['run_id'])
        runs=list_imports(self.store)
        self.assertEqual(len(runs),2)
        by_agent={r['details']['executor']['agent']:r for r in runs}
        self.assertEqual(by_agent['Agent A']['details']['messages'],dict(inserted=3,revised=0,unchanged=0))
        self.assertEqual(by_agent['Agent B']['details']['messages'],dict(inserted=0,revised=0,unchanged=3))
        self.assertEqual(by_agent['Agent A']['details']['stored_by_category']['source_payload'],3)
        self.assertEqual([e['event'] for e in events(self.store,begun['run_id'])],['started','complete'])
        with self.store.connect() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM import_events')

    def test_host_jobs_log_once_and_preserve_counts_across_batches(self):
        self.ws.register_source('host','Host','claude-code','machine')
        job=self.ws.stage_records('host',[event(message_id=str(i)) for i in range(3)],'batch')
        for _ in range(4):
            self.ws.run_import(job['id'],1)
        runs=list_imports(self.store)
        self.assertEqual(len(runs),1)
        self.assertEqual(runs[0]['details']['messages']['inserted'],3)
        self.assertEqual(len(events(self.store,job['id'])),2)

    def mixed(self):
        rows=[{'type':'user','uuid':'a','sessionId':'first','message':{'content':'question'}},
              {'type':'custom-title','sessionId':'first','customTitle':'First title'},
              {'type':'relocated','sessionId':'second','relocatedCwd':'/new/path'},
              {'type':'assistant','uuid':'b','sessionId':'second','parentUuid':'a','message':{'content':'answer'}},
              {'type':'ai-title','sessionId':'second','aiTitle':'Second AI (fork)'},
              {'type':'custom-title','sessionId':'second','customTitle':'Chosen title'}]
        path=self.root/'mixed.jsonl'
        path.write_text('\n'.join(map(canonical,rows))+'\n',encoding='utf-8')
        return path,rows

    def test_mixed_native_sessions_are_selected_independently_without_inventing_forks(self):
        path,rows=self.mixed()
        groups=discover(path)
        self.assertEqual(len(groups),2)
        second=next(g for g in groups if g['conversation_id']=='second')
        self.assertEqual(second['title'],'Chosen title')
        self.assertTrue(second['transitions'])
        records,_=convert([path],'machine',session_id='second')
        self.assertTrue(all(r['conversation_id']=='second' for r in records))
        self.assertFalse(any(r.get('fork_of') for r in records))
        self.assertEqual(len(records),4)

    def test_title_history_changes_without_revising_existing_messages(self):
        path,rows=self.mixed()
        records,_=convert([path],'machine')
        self.store.ingest(records)
        before=self.store.stats()['revisions']
        rows.append({'type':'custom-title','sessionId':'second','customTitle':'Renamed'})
        path.write_text('\n'.join(map(canonical,rows))+'\n',encoding='utf-8')
        new,_=convert([path],'machine')
        result=self.store.ingest(new)
        self.assertEqual(result['revised'],0)
        self.assertEqual(self.store.stats()['revisions'],before+1)
        # Replaying the older snapshot must not roll the display title back.
        self.store.ingest(records)
        session=next(s for s in list_sessions(self.store) if s['native_id']=='second')
        self.assertEqual(session['details']['display_title'],'Renamed')
        self.assertEqual(len(session['details']['titles']),3)

    def test_legacy_mixed_group_preserves_message_keys_and_continues_without_duplicates(self):
        old=event(conversation_id='first',message_id='b',title='Legacy title',raw={'sessionId':'second'})
        self.store.ingest([old])
        with self.store.connect() as con:
            con.execute("DELETE FROM metadata WHERE key='control_version'")
        Store(self.store.root)
        updated=event(conversation_id='second',message_id='b',title='New title',raw={'sessionId':'second'},
            conversation_metadata={'native_id':'second','display_title':'New title','titles':[{'kind':'customTitle','value':'New title'}]})
        result=self.store.ingest([updated])
        self.assertEqual(result['inserted'],0)
        self.assertEqual(result['revised'],0)
        self.assertEqual(self.store.stats()['messages'],1)
        self.assertEqual(self.store.read(key('claude-code','machine','first','b'))['title'],'Legacy title')
        self.store.ingest([dict(updated,message_id='c')])
        self.assertEqual(self.store.stats()['conversations'],1)
        self.assertEqual(list_sessions(self.store)[0]['conversation_id'],'first')

    def test_session_metadata_survives_capture_import_and_replay(self):
        path,_=self.mixed()
        records,_=convert([path],'machine')
        packet,m=self.packet(records)
        first=import_capture(self.store,packet,[self.root])
        second=import_capture(self.store,packet,[self.root])
        self.assertEqual(second['messages']['revised'],0)
        self.assertEqual(second['messages']['unchanged'],len(records))
        self.assertEqual(len(list_sessions(self.store)),2)
        with self.store.connect() as con:
            self.assertTrue(con.execute("SELECT 1 FROM graph_edges WHERE predicate='observed_after_session'").fetchone())

    def test_transfer_failure_keeps_import_counts_and_resume_event_history(self):
        path,m=self.packet([event(payloads=[{'kind':'tool_result','value':'bytes'}])])
        up,begun=self.upload(path,m)
        with patch.object(Catalog,'store_attachment',side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                up.finish(begun['upload_id'],run_id=begun['run_id'])
        self.assertEqual(list_imports(self.store)[0]['status'],'failed')
        up.finish(begun['upload_id'],run_id=begun['run_id'])
        self.assertEqual(list_imports(self.store)[0]['details']['messages']['inserted'],1)
        self.assertEqual([e['event'] for e in events(self.store,begun['run_id'])],['started','failed','complete'])

    def test_folder_policy_refuses_unknown_cwd_and_normalizes_parent_segments(self):
        p=Policy({'version':1,'rules':[{'source':'claude-code','root':'C:/work','permissions':'exclude'}]})
        self.assertFalse(p.select(event(raw={'cwd':'C:/personal/../work/project'}))[0]['conversation'])
        with self.assertRaisesRegex(ValueError,'working directory'):
            p.select(event())
        with self.assertRaises(ValueError):
            Policy({})

    def test_direct_mcp_target_is_fixed_at_preflight(self):
        project=self.cat.create_project('target','Target')
        path,m=self.packet()
        up,begun=self.upload(path,m,project_id=project['id'])
        with self.assertRaisesRegex(ValueError,'Target project'):
            up.finish(begun['upload_id'])
        self.assertTrue(up.finish(begun['upload_id'],project['id'],begun['run_id'])['complete'])

    def test_group_prefix_preview_never_changes_project(self):
        p=self.cat.create_project('p','P','Группа: Личные проекты. Details')
        preview=self.cat.group_suggestions()
        self.assertEqual(preview['suggestions'][0]['suggested_group_title'],'Личные проекты')
        self.assertEqual(self.cat.groups(),[])
        self.assertEqual(self.cat.project(p['id'])['summary'],p['summary'])

    def test_send_resumes_interrupted_run_and_starts_new_run_after_success(self):
        from portable_context.client import send
        path,m=self.packet()
        profile=self.root/'profile.json'
        profile.write_text(canonical({'workspace_id':self.ws.describe()['workspace_id'],'connection':{}}))
        up=Uploads(self.ws)
        fail=[True]
        async def connected(entry,operation):
            return await operation(None)
        async def invoked(session,name,args=None):
            args=args or {}
            if name=='memory_status': return self.ws.describe()
            if name=='begin_capture_upload': return up.begin(**args)
            if name=='capture_upload_status': return up.status(args['upload_id'],args['offset'],args['limit'])
            if name=='upload_capture_chunks':
                if fail[0]:
                    fail[0]=False
                    raise OSError('private error must not appear in local receipt')
                return up.put(args['upload_id'],args['chunks'])
            if name=='finish_capture_upload': return up.finish(args['upload_id'],args.get('project_id'),args.get('run_id'))
            self.fail(name)
        with patch('portable_context.client.with_session',side_effect=connected),patch('portable_context.client.call',side_effect=invoked):
            with self.assertRaises(OSError): asyncio.run(send(profile,path))
            self.assertNotIn('private error',(self.root/'last-delivery.json').read_text())
            self.assertTrue(asyncio.run(send(profile,path))['complete'])
            self.assertEqual(len(list_imports(self.store)),1)
            self.assertTrue(asyncio.run(send(profile,path))['complete'])
            self.assertEqual(len(list_imports(self.store)),2)

    def test_import_scope_is_bounded_and_host_executor_is_retained(self):
        from portable_context.import_log import scope
        self.ws.register_source('host','Host','claude-code','machine')
        job=self.ws.stage_records('host',[event(conversation_id=str(i)) for i in range(30)],'scope',
            executor={'client':'bridge','agent':'Claude','version':'1'})
        self.ws.run_import(job['id'])
        result=list_imports(self.store)[0]
        self.assertEqual(result['details']['executor']['agent'],'Claude')
        self.assertEqual(len(result['details']['conversations']),20)
        self.assertEqual(result['details']['conversations_count'],30)
        self.assertEqual(scope(self.store,job['id'],limit=20)['next_offset'],20)
        self.assertEqual(len(scope(self.store,job['id'],offset=20)['items']),10)


if __name__=='__main__':
    unittest.main()
