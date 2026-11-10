import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import https_setup as setup
import https_runtime as runtime
from portable_context.core import Store
from portable_context.workspace import Workspace


class HTTPSSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_resume_preserves_intent_and_never_marks_failed_stage_complete(self):
        journal=setup.Journal(self.root,{'target':'one'})
        ids=[]
        def fail(intent):ids.append(intent['id']);raise RuntimeError('lost reply')
        with self.assertRaises(RuntimeError):journal.stage('create',fail)
        again=setup.Journal(self.root,{'target':'one'})
        self.assertNotIn('create',again.value['completed'])
        value=again.stage('create',lambda intent:{'id':intent['id']})
        self.assertEqual(value['id'],ids[0])
        self.assertEqual(again.stage('create',lambda _:self.fail('Repeated mutation')),value)
        with self.assertRaises(ValueError):setup.Journal(self.root,{'target':'other'})

    def approval_fixture(self):
        setup.save(self.root/'deployment.json',{'host':'192.0.2.4'})
        folder=self.root/'https-firewall';folder.mkdir();(folder/'main.tf').write_text('reviewed rule')
        (folder/'https.tfplan').write_bytes(b'plan')
        value={'config':{},'deployment_sha256':setup.sha(self.root/'deployment.json'),
            'firewall_plan_sha256':setup.sha(folder/'https.tfplan'),'firewall_files_sha256':setup.firewall_fingerprint(folder)}
        value['plan_sha256']=setup.digest(value);setup.save(self.root/'https-plan.json',value)
        return value

    def test_approval_is_exact_and_persists_only_for_unchanged_target(self):
        review=self.approval_fixture()
        with self.assertRaises(ValueError):setup.approved_plan(self.root,None)
        setup.approved_plan(self.root,review['plan_sha256'])
        self.assertEqual(setup.approved_plan(self.root,None),review)
        setup.save(self.root/'deployment.json',{'host':'192.0.2.5'})
        with self.assertRaises(ValueError):setup.approved_plan(self.root,None)

    def test_approval_rejects_modified_terraform_and_plan_document(self):
        review=self.approval_fixture();setup.approved_plan(self.root,review['plan_sha256'])
        (self.root/'https-firewall/main.tf').write_text('open all ports')
        with self.assertRaises(ValueError):setup.approved_plan(self.root,None)
        review['config']={'other':'target'};setup.save(self.root/'https-plan.json',review)
        with self.assertRaises(ValueError):setup.approved_plan(self.root,None)

    def manifest(self):
        Workspace(Store(self.root/'memory'));identifier=runtime.workspace(self.root)
        manifest={'format':1,'workspace_id':identifier,'runtime':str(self.root/'oauth10'),'origin':'https://memory.example',
            'application_port':8767,'identity_service':'pc-test-identity.service','mcp_service':'pc-test-mcp.service'}
        setup.save(self.root/'https-runtime.json',manifest)
        directory=self.root/'oauth10';directory.mkdir()
        setup.save(directory/'oauth.json',{'issuer':'https://memory.example','resource':'https://memory.example/mcp'})
        return manifest

    def test_update_rebinds_http_to_current_and_failure_closes_writers(self):
        manifest=self.manifest()
        with patch.object(runtime,'service') as service,patch.object(runtime,'run'),patch.object(runtime,'metadata',side_effect=ValueError('not ready')):
            with self.assertRaises(ValueError):runtime.start_mcp(self.root,manifest)
        runner=(self.root/'oauth10/run-mcp').read_text().replace('\\','/')
        self.assertIn('current/venv/bin/python',runner)
        self.assertNotIn('oauth10/venv/bin/python',runner)
        self.assertIn('service.lock',runner);self.assertIn('maintenance.json',runner)
        self.assertTrue((self.root/'maintenance.json').exists())
        self.assertEqual(service.call_args.kwargs['requires'],'pc-test-identity.service')

    def test_wrong_workspace_never_stops_service(self):
        manifest=self.manifest();manifest['workspace_id']='other';setup.save(self.root/'https-runtime.json',manifest)
        with patch.object(runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.quiesce(self.root)
            run.assert_not_called()

    def test_server_rejects_changed_origin_before_service_mutation(self):
        manifest=self.manifest()
        with patch.object(runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.configure(self.root,manifest['workspace_id'],'https://another.example',self.root/'missing')
            run.assert_not_called()

    def test_owner_code_handoff_is_private_and_redacted(self):
        setup.save(self.root/'https-installed.json',{'origin':'https://memory.example'})
        secret={'code':'Secret123456','login_url':'https://memory.example/login','expires_in_minutes':20}
        with patch.object(setup,'remote',return_value={'private_login':secret}):receipt=setup.owner_login(self.root)
        self.assertNotIn('Secret123456',json.dumps(receipt))
        self.assertEqual(json.loads(Path(receipt['private_file']).read_text()),secret)
        if os.name!='nt':self.assertEqual(Path(receipt['private_file']).stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
