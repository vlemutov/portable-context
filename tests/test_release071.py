import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from portable_context.core import Store, canonical, key
from portable_context.workspace import Workspace
from portable_context.claude_code import convert, discover, local_path_status
from portable_context.capture import capture, import_capture
from portable_context.attribution import attribution
from portable_context.graph import read_graph
from portable_context.wiki import apply_pages, lint, read_page


def event(cid='s', mid='m', **kwargs):
    return dict(source='claude-code', instance='fixture', conversation_id=cid, message_id=mid,
                role='user', text='summaryneedle', **kwargs)


class Hotfix071(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory')
        self.ws=Workspace(self.store)

    def source_history(self):
        with self.store.connect() as con:
            return [tuple(r) for r in con.execute('SELECT id,hash,record FROM revisions ORDER BY id,hash')]

    def test_native_metadata_authoritative_and_partial_rows_preserve_pagination(self):
        for cid in ('a','b','c'):
            self.store.ingest([event(cid,mid=cid,conversation_metadata={'native_id':cid,'display_title':cid})])
        with self.store.connect() as con:
            con.execute("INSERT INTO source_sessions VALUES ('claude-code','fixture','b2','b',?)",
                        (canonical({'native_id':'wrong','display_title':'Alias'}),))
        before=self.source_history()
        item=self.store.conversations(limit=1,offset=1)[0]
        self.assertEqual([s['native_id'] for s in item['native_sessions']],['b','b2'])
        self.assertEqual(item['messages'],1)
        for broken in ('{','[]',canonical({'display_title':4})):
            with self.store.connect() as con:
                con.execute("UPDATE source_sessions SET details=? WHERE native_id='b'",(broken,))
            item=self.store.conversations(limit=1,offset=1)[0]
            self.assertTrue(item['partial'])
            self.assertEqual(item['diagnostics'][0]['position'],1)
            self.assertEqual(item['conversation'],key('claude-code','fixture','b'))
            self.assertEqual(self.store.conversations(limit=1,offset=2)[0]['title'],'c')
            self.store.project()
        with self.store.connect() as con:
            con.execute("DELETE FROM source_sessions WHERE conversation_id='a'")
        self.assertEqual(self.store.conversations(limit=1)[0]['native_sessions'],[])
        self.assertEqual(before,self.source_history())
        with self.store.connect() as con:
            con.execute("UPDATE messages SET record='{}' WHERE message_id='b' AND conversation=?",(key('claude-code','fixture','b'),))
        self.assertTrue(self.store.conversations(limit=1,offset=1)[0]['partial'])
        with patch.object(self.store,'connect',side_effect=sqlite3.OperationalError('database unavailable')):
            with self.assertRaises(sqlite3.OperationalError): self.store.conversations()

    def test_existing_compact_summary_is_derived_across_recall_without_revisions(self):
        original=event(raw={'isCompactSummary':True,'isVisibleInTranscriptOnly':True})
        self.store.ingest([original,event(mid='ordinary'),event(mid='visible',raw={'isVisibleInTranscriptOnly':True})])
        before=self.source_history()
        mid=key('claude-code','fixture','s','m')
        record=self.store.read(mid)
        self.assertEqual((record['source_role'],record['role'],record['content_kind']),('user','assistant','generated_summary'))
        self.assertEqual(next(x for x in self.store.search('summaryneedle') if x['id']==mid)['role'],'assistant')
        cid=self.store.conversations()[0]['conversation']
        records=self.store.conversation(cid)
        self.assertEqual([r['role'] for r in records],['assistant','user','user'])
        exact=self.ws.read_revision(mid,record['revision'])
        self.assertEqual(json.loads(exact['json_fragment'])['role'],'user')
        self.assertEqual(exact['effective_role'],'assistant')
        job=self.ws.start_enrichment()
        task=self.ws.claim_enrichment(job['id'],'test')['task']
        self.assertEqual(task['previews'][0]['role'],'assistant')
        self.store.project()
        text=(self.store.root/'vault/conversations'/f'{cid}.md').read_text(encoding='utf-8')
        self.assertIn('assistant / generated_summary',text)
        graph=read_graph(self.store,'message:'+mid)
        self.assertEqual(next(n for n in graph['nodes'] if n['id']=='message:'+mid)['data']['role'],'assistant')
        self.assertEqual(self.store.ingest([original])['unchanged'],1)
        self.assertEqual(before,self.source_history())
        self.assertEqual(attribution(event(raw={'isCompactSummary':'true'}))['effective_role'],'user')

    def test_archived_raw_verifies_hash_and_missing_bytes_do_not_imply_human(self):
        packet=self.root/'packet'
        capture([event(raw={'isCompactSummary':True,'padding':'x'*34000})],packet,[self.root])
        import_capture(self.store,packet,[self.root])
        mid=key('claude-code','fixture','s','m')
        r=self.store.read(mid)
        self.assertEqual(r['content_kind'],'generated_summary')
        sha=next(a['sha256'] for a in r['attachments'] if a['kind']=='full_raw')
        original=self.store.root/'originals'/sha
        original.write_bytes(b'{}')
        self.assertEqual(self.store.read(mid)['effective_role'],'unknown')
        original.unlink()
        self.assertEqual(self.store.read(mid)['effective_role'],'unknown')

    def test_wiki_old_claim_flagged_not_rewritten_new_claim_rejected(self):
        self.store.ingest([event(raw={'isCompactSummary':True})])
        r=self.store.read(key('claude-code','fixture','s','m'))
        ref={'message_key':r['key'],'revision':r['revision']}
        page={'id':'legacy','title':'Legacy','text':'User requested X','kind':'user_statement','sources':[ref]}
        with self.store.connect() as con:
            with patch('portable_context.wiki.citation_warnings',return_value=[]):
                apply_pages(self.ws,con,[page],'legacy-agent')
        self.ws.refresh()
        before=read_page(self.ws,'legacy')
        self.assertTrue(before['attribution_warnings'])
        self.assertEqual(lint(self.ws)['summary']['user_statement_attribution_needs_review'],1)
        with self.store.connect() as con:
            with self.assertRaisesRegex(ValueError,'Generated summaries'):
                apply_pages(self.ws,con,[{**page,'id':'new'}],'new-agent')
        after=read_page(self.ws,'legacy')
        self.assertEqual((before['version'],before['body']),(after['version'],after['body']))
        self.assertIn('user attribution needs review',(self.store.root/'vault/wiki/INDEX.md').read_text(encoding='utf-8'))

    def transcript(self,directory,name,rows):
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/name
        path.write_text('\n'.join(canonical(r) for r in rows),encoding='utf-8')
        return path

    def test_late_cwd_relocation_and_duplicate_record_counts(self):
        directory=self.root/'projects/encoded'
        rows=[{'sessionId':'s','uuid':'first','type':'user','message':{'content':'hello'}},
              {'sessionId':'s','uuid':'second','cwd':str(self.root),'type':'user','message':{'content':'hello'}},
              {'sessionId':'s','uuid':'third','type':'relocated','relocatedCwd':str(self.root/'missing')}]
        first=self.transcript(directory,'one.jsonl',rows)
        self.transcript(directory,'two.jsonl',rows+[{'sessionId':'s','uuid':'last','cwd':str(self.root)}])
        (directory/'memory').mkdir()
        result=discover(self.root/'projects')[0]
        self.assertEqual(result['records'],4)
        self.assertEqual(result['cwd'],str(self.root))
        by_path={w['path']:w for w in result['workspaces']}
        self.assertEqual(by_path[str(self.root)]['message_count'],2)
        self.assertEqual(by_path[str(self.root/'missing')]['message_count'],0)
        self.assertEqual(by_path[str(self.root/'missing')]['local_status'],'missing')
        self.assertEqual(result['memory_roots'],[])
        self.assertEqual(result['memory_candidates'][0]['ownership'],'ambiguous')
        records,_=convert([first],'fixture')
        self.assertNotIn('workspace_observations',records[0]['conversation_metadata'])

    def test_colliding_encoded_folder_detected_across_distinct_sessions(self):
        directory=self.root/'projects/encoded---'
        for sid,name in [('a','Проект'),('b','Другой')]:
            self.transcript(directory,sid+'.jsonl',[{'sessionId':sid,'uuid':'m','cwd':str(self.root/name),'type':'user'}])
        (directory/'memory').mkdir()
        for result in discover(self.root/'projects'):
            self.assertEqual(result['memory_roots'],[])
            self.assertEqual(len(result['memory_candidates'][0]['workspace_candidates']),2)
            self.assertTrue(result['memory_candidates'][0]['requires_explicit_binding'])

    def test_cross_platform_paths_and_unreadable_are_not_missing(self):
        foreign='/Users/alice/project' if os.name=='nt' else 'C:\\Users\\alice\\project'
        self.assertEqual(local_path_status(foreign),'foreign_platform')
        with patch.object(Path,'stat',side_effect=PermissionError('denied')):
            self.assertEqual(local_path_status(str(self.root)),'unreadable')
        self.assertEqual(local_path_status('relative/path'),'relative_unresolved')

    def test_new_adapter_keeps_source_envelope_and_flags(self):
        path=self.transcript(self.root/'source','s.jsonl',[{'sessionId':'s','uuid':'m','type':'user',
            'isCompactSummary':True,'isVisibleInTranscriptOnly':True,'message':{'content':'generated summary'}}])
        records,_=convert([path],'fixture')
        self.assertEqual(records[0]['role'],'user')
        self.assertTrue(records[0]['raw']['isCompactSummary'])
        self.assertEqual(attribution(records[0])['effective_role'],'assistant')
