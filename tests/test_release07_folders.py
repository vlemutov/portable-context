import json,tempfile,unittest
from pathlib import Path
from portable_context.core import Store,canonical
from portable_context.catalog import Catalog,summary
from portable_context.workspace import Workspace
from portable_context.project_files import preview,bind,prepare
from portable_context.capture import import_capture
from portable_context.policy import Policy,set_policy

class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.store=Store(self.root/'memory'); self.ws=Workspace(self.store,[self.root]); self.cat=Catalog(self.store)
        self.project=self.cat.create_project('fixture','Fixture')
        self.profile=self.root/'profile.json'; self.profile.write_text(canonical({'workspace_id':self.ws.describe()['workspace_id']}))
        self.folder=self.root/'project'; self.folder.mkdir(); (self.folder/'report.md').write_text('Useful result needle')
        (self.folder/'.env').write_text('PASSWORD=never capture')
        (self.folder/'node_modules').mkdir(); (self.folder/'node_modules/cache.js').write_text('dependency')

    def binding(self,**kw):
        plan=preview(self.folder,self.project['id'],self.ws.describe()['workspace_id'],**kw)
        return bind(self.profile,self.folder,self.project['id'],plan['plan_id'],**kw)

    def packet(self,binding,name):
        packet=self.root/name; prepare(self.profile,binding['id'],packet)
        return packet

    def test_project_files_have_passports_without_invented_messages(self):
        binding=self.binding(); packet=self.packet(binding,'first')
        result=import_capture(self.store,packet,[self.root])
        self.assertTrue(result['complete']); self.assertEqual(self.store.stats()['messages'],0)
        self.assertEqual(len(self.cat.files(self.project['id'])),2)
        found=self.cat.search('needle',self.project['id']); self.assertEqual(len(found),1)
        files=self.cat.files(self.project['id'])
        doc=next(f for f in files if f['name']=='report.md')
        self.assertEqual(doc['occurrences'][0]['message_key'],'')
        self.assertIn('report.md',self.cat.project(self.project['id'])['entry'])
        self.assertEqual(summary(self.store)['files'],2)
        self.assertTrue((packet/'catalog/project.md').exists())

    def test_versions_absence_and_old_replay_preserve_current(self):
        binding=self.binding(); first=self.packet(binding,'first'); import_capture(self.store,first,[self.root])
        fid=next(f for f in self.cat.files() if f['name']=='report.md')['id']
        (self.folder/'report.md').write_text('Changed result')
        second=self.packet(binding,'second'); import_capture(self.store,second,[self.root])
        self.assertEqual(len(self.cat.file(fid)['versions']),2)
        (self.folder/'report.md').unlink()
        third=self.packet(binding,'third'); import_capture(self.store,third,[self.root])
        self.assertFalse(any(f['id']==fid for f in self.cat.files()))
        import_capture(self.store,first,[self.root])
        self.assertFalse(any(f['id']==fid for f in self.cat.files()))
        self.assertEqual(len(self.cat.file(fid)['versions']),2)

    def test_stale_plan_and_policy(self):
        plan=preview(self.folder,self.project['id'],self.ws.describe()['workspace_id'])
        (self.folder/'new.txt').write_text('new')
        with self.assertRaisesRegex(ValueError,'changed'): bind(self.profile,self.folder,self.project['id'],plan['plan_id'])
        binding=self.binding(); packet=self.packet(binding,'packet')
        set_policy(self.store,'no_document_bytes','user',project_id=self.project['id'])
        with self.assertRaisesRegex(ValueError,'policy'): import_capture(self.store,packet,[self.root])
        self.assertEqual(self.cat.files(),[])

    def test_late_unseen_snapshot_and_reappearing_file(self):
        binding=self.binding(); old=self.packet(binding,'old')
        (self.folder/'report.md').write_text('Newest evidence')
        new=self.packet(binding,'new'); import_capture(self.store,new,[self.root])
        import_capture(self.store,old,[self.root])
        self.assertTrue(self.cat.search('Newest')); self.assertFalse(self.cat.search('needle'))
        (self.folder/'report.md').unlink(); absent=self.packet(binding,'absent'); import_capture(self.store,absent,[self.root])
        (self.folder/'report.md').write_text('Newest evidence')
        back=self.packet(binding,'back'); import_capture(self.store,back,[self.root])
        self.assertTrue(self.cat.search('Newest'))
        from portable_context.graph import read_graph
        read_graph(self.store)

    def test_project_upload_resumes_and_current_policy_wins(self):
        import base64
        from portable_context.transfer import Uploads
        for i in range(28): (self.folder/(str(i)+'.txt')).write_text('Original '+str(i))
        binding=self.binding(); packet=self.packet(binding,'upload'); manifest=json.loads((packet/'manifest.json').read_text())
        up=Uploads(self.ws); started=up.begin(manifest)
        for part in manifest['parts']+manifest['blobs']:
            up.put(started['upload_id'],[{'path':part['path'],'offset':0,'content_base64':base64.b64encode((packet/part['path']).read_bytes()).decode()}])
        first=up.finish(started['upload_id'],run_id=started['run_id']); self.assertFalse(first['complete'])
        second=up.finish(started['upload_id'],run_id=started['run_id']); self.assertTrue(second['complete'])
        self.assertEqual(second['stored_attachment_occurrences'],29)
        repeated=self.packet(binding,'repeated'); repeated_manifest=json.loads((repeated/'manifest.json').read_text())
        reused=up.begin(repeated_manifest)
        self.assertEqual(reused['reused_blobs'],29)
        self.assertTrue(all(x['verified'] for x in up.status(reused['upload_id'])['files'] if x['path'].startswith('blobs/')))
        self.assertEqual(self.store.stats()['messages'],0)
        denied=Policy({'version':1,'default':'exclude','rules':[]})
        clean=self.root/'denied'; prepare(self.profile,binding['id'],clean,policy=denied)
        self.assertEqual(json.loads((clean/'manifest.json').read_text())['project_snapshot']['files'],[])

    def test_root_selection_and_new_snapshot_are_explicit(self):
        with self.assertRaises(ValueError): preview(Path.home(),'p','w')
        binding=self.binding()
        with self.assertRaisesRegex(ValueError,'outside'): prepare(self.profile,binding['id'],self.folder/'capture')
        denied=Policy({'version':1,'default':'exclude','rules':[]})
        plan=preview(self.folder,self.project['id'],self.ws.describe()['workspace_id'],policy=denied)
        self.assertEqual(plan['selected_bytes'],0)

if __name__=='__main__': unittest.main()
