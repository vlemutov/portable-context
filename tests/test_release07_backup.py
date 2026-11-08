import json,os,secrets,shutil,sqlite3,tempfile,unittest
from contextlib import closing
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from portable_context.core import Store,canonical
from portable_context.workspace import Workspace
from portable_context.restic_backup import Manager,retention,DEFAULT_POLICY

def record(i=0):
    return dict(source='events',instance='fixture',conversation_id='c',message_id=str(i),role='user',text='Synthetic message '+str(i))

class RetentionTests(unittest.TestCase):
    def test_union_and_pins(self):
        clock=datetime(2026,11,4,tzinfo=timezone.utc)
        items=[{'id':str(i),'time':(clock-timedelta(days=i*10)).isoformat()} for i in range(50)]
        items[20]['checkpoint_at']=clock.isoformat()
        result=retention(items,dict(last=2,weekly=2,monthly=2,checkpoints_days=90),['49'],clock)
        self.assertTrue({'0','1','20','49'}<=set(result['keep']))
        self.assertTrue(result['remove'])
        self.assertFalse(set(result['keep'])&set(result['remove']))

@unittest.skipUnless(os.environ.get('PC_TEST_RESTIC'),'Set PC_TEST_RESTIC to a verified test binary')
class ResticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory'); Workspace(self.store)
        self.store.ingest([record()])
        password=self.root/'password'; password.write_text(secrets.token_urlsafe(32))
        config=self.root/'config.json'; config.write_text(canonical(dict(state=str(self.root/'state'),
            binary=os.environ['PC_TEST_RESTIC'],repository=str(self.root/'repository'),password_file=str(password))))
        self.manager=Manager(self.store.root,config); self.manager.initialize()

    def test_no_change_originals_and_self_contained_restore(self):
        f=self.root/'document.txt'; f.write_text('original content')
        original=self.store.archive(f)
        first=self.manager.checkpoint('before_import','run-1')
        second=self.manager.checkpoint()
        self.assertEqual(second['status'],'skipped')
        self.assertEqual(len(self.manager.snapshots()),1)
        self.store.ingest([record(1)])
        third=self.manager.checkpoint('before_import','run-2')
        self.assertEqual(third['status'],'created')
        self.assertEqual(self.manager.checkpoint('before_import','run-2')['status'],'reused')
        self.assertEqual(len(self.manager.snapshots()),2)
        f.unlink(); (self.store.root/'originals'/original).unlink()
        sid=self.manager.snapshots()[0]['id']
        restored=self.manager.restore(sid,self.root/'restored')
        self.assertTrue(restored['verified'])
        self.assertEqual((self.root/'restored/originals'/original).read_text(),'original content')

    def test_corrupt_original_blocks_snapshot(self):
        f=self.root/'document.txt'; f.write_text('original')
        checksum=self.store.archive(f)
        (self.store.root/'originals'/checksum).write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'corrupt'): self.manager.checkpoint('before_import','bad')
        self.assertEqual(self.manager.snapshots(),[])
        self.assertIsNotNone(self.manager.status()['latest']['checkpoint_failed'])

    def test_post_import_exact_coverage_and_cache_cleanup(self):
        from portable_context.capture import capture,import_capture
        from portable_context.maintenance import cleanup_preview
        before=self.manager.post_import()
        self.assertTrue(before['coverage_verified'])
        packet=self.root/'imports/legacy-package'
        capture([dict(record(10),payloads=[{'kind':'tool_result','value':'backed up bytes'}])],packet)
        import_capture(self.store,packet,[self.root])
        with patch('portable_context.maintenance.configured',return_value=self.manager):
            plan=cleanup_preview(self.root)
            self.assertEqual(plan['items'],[])
            self.assertEqual(plan['kept'][0]['reason'],'no_verified_snapshot_covering_contents')
            after=self.manager.post_import()
            self.assertNotEqual(before['snapshot_id'],after['snapshot_id'])
            plan=cleanup_preview(self.root)
            self.assertEqual(plan['items'][0]['snapshot_id'],after['snapshot_id'])
        self.assertEqual(self.manager.cache_cleanup(30)['policy_max_age_days'],30)

    def test_policy_preview_confirmation_and_protected_restore(self):
        self.manager.checkpoint()
        self.store.ingest([record(1)]); self.manager.checkpoint()
        values=dict(last=1,weekly=0,monthly=0,checkpoints_days=0)
        plan=self.manager.preview(values)
        self.assertEqual(len(plan['remove']),1)
        with self.assertRaisesRegex(ValueError,'Confirm'): self.manager.set_policy(values,1,'user')
        self.manager.set_policy(values,1,'user',plan['confirmation'])
        self.assertEqual(len(self.manager.snapshots()),2)
        with self.manager.connect() as con: con.execute('INSERT INTO pins VALUES (?,?)',(plan['remove'][0],'restore'))
        self.assertEqual(self.manager.prune()['removed_snapshots'],[])
        with self.manager.connect() as con: con.execute('DELETE FROM pins')
        self.assertEqual(len(self.manager.prune()['removed_snapshots']),1)

    def test_errors_do_not_disclose_credentials(self):
        import subprocess
        result=subprocess.CompletedProcess([],1,stdout='private-token-value',stderr='password=private-token-value')
        with patch('subprocess.run',return_value=result):
            with self.assertRaises(ValueError) as error: self.manager.run(['snapshots'])
        self.assertNotIn('private-token-value',str(error.exception))
        self.assertNotIn('private-token-value',canonical(self.manager.status()))

    def test_managed_import_failure_blocks_and_resume_shares_checkpoint(self):
        from portable_context.capture import capture,import_capture
        packet=self.root/'packet'; capture([record(2)],packet,[self.root])
        with patch.dict(os.environ,{'PC_BACKUP_CONFIG':str(self.manager.config_path)}):
            with patch.object(Manager,'run',side_effect=ValueError('offline')):
                with self.assertRaisesRegex(ValueError,'offline'): import_capture(self.store,packet,[self.root],run_id='fixed')
            self.assertEqual(self.store.stats()['messages'],1)
            import_capture(self.store,packet,[self.root],run_id='fixed')
            before=len(self.manager.snapshots())
            import_capture(self.store,packet,[self.root],run_id='fixed')
            self.assertEqual(len(self.manager.snapshots()),before)
            with self.manager.connect() as con: self.assertEqual(con.execute("SELECT count(*) FROM operations WHERE id='fixed'").fetchone()[0],1)
            self.manager.bypass_once('explicit','owner','Offline emergency','IMPORT WITHOUT BACKUP')
            with patch.object(Manager,'run',side_effect=ValueError('offline')):
                self.assertEqual(self.manager.checkpoint('before_import','explicit')['status'],'explicit_bypass')

    def test_activation_rejects_tampered_original_then_preserves_old_memory(self):
        from portable_context.maintenance import activation_preview,activate
        f=self.root/'doc'; f.write_text('valuable original'); sha=self.store.archive(f)
        snap=self.manager.checkpoint(); target=self.root/'restored'
        self.manager.restore(snap['snapshot_id'],target)
        (target/'originals'/sha).write_text('tampered')
        with self.assertRaisesRegex(ValueError,'changed'): activation_preview(self.root,target)
        (target/'originals'/sha).write_text('valuable original')
        plan=activation_preview(self.root,target)
        with patch.dict(os.environ,{'PC_BACKUP_CONFIG':str(self.manager.config_path)}):
            result=activate(self.root,target,plan['confirmation'])
        self.assertTrue(Path(result['previous_directory']).is_dir())
        self.assertEqual((self.root/'memory/originals'/sha).read_text(),'valuable original')

if __name__=='__main__': unittest.main()
