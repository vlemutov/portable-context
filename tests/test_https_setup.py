import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
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

    def test_partial_bootstrap_does_not_stop_or_start_missing_writer(self):
        manifest=self.manifest();unit=self.root/'absent.service'
        for has_binding in (True,False):
            if not has_binding:(self.root/'oauth10/oauth.json').unlink()
            with patch.object(runtime,'unit_file',return_value=unit),patch.object(runtime,'run') as run:
                self.assertIsNone(runtime.update_manifest(self.root))
                self.assertIsNone(runtime.quiesce(self.root));run.assert_not_called()
                self.assertEqual(runtime.describe(self.root),manifest)

    def test_missing_binding_or_foreign_unit_prevents_update(self):
        self.manifest();unit=self.root/'existing.service'
        unit.write_text(str(self.root));(self.root/'oauth10/oauth.json').unlink()
        with patch.object(runtime,'unit_file',return_value=unit),patch.object(runtime,'run') as run:
            with self.assertRaisesRegex(ValueError,'lost its OAuth'):runtime.quiesce(self.root)
            unit.write_text('another-installation')
            with self.assertRaisesRegex(ValueError,'another installation'):runtime.quiesce(self.root)
            run.assert_not_called()

    def test_systemd_failure_also_closes_maintenance(self):
        manifest=self.manifest()
        with patch.object(runtime,'service',side_effect=RuntimeError('systemd unavailable')),patch.object(runtime,'run',side_effect=RuntimeError('also unavailable')):
            with self.assertRaisesRegex(RuntimeError,'systemd unavailable'):runtime.start_mcp(self.root,manifest)
        self.assertEqual(setup.read(self.root/'maintenance.json')['operation'],'https-readiness')

    def test_existing_pilot_gets_connection_receipt_without_login_or_cloud_mutation(self):
        setup.save(self.root/'installed.json',{'https':{'ready':True,'origin':'https://memory.example'},'memory':{'workspace_id':'pilot'}})
        with patch.object(setup,'remote',return_value={'workspace_id':'pilot'}) as remote,patch.object(setup,'check_public',return_value={'ready':True}),patch.object(setup,'CloudAPI') as cloud:
            receipt=setup.connection_receipt(self.root)
            self.assertEqual(receipt['workspace_id'],'pilot');remote.assert_called_once_with(self.root,'check','https://memory.example')
            self.assertEqual(setup.connection_receipt(self.root),receipt);cloud.assert_not_called()
        self.assertTrue((self.root/'client-https.json').exists())
        self.assertFalse((self.root/'owner-enrollment.json').exists())

    def test_existing_application_is_never_adopted_as_placeholder(self):
        app={'image':'pinned','command':['caddy'],'args':['run'],'env':[]}
        with self.assertRaisesRegex(ValueError,'another application'):
            setup.validate_placeholder({'template':{'containers':[app]}},'pinned')

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

    def test_lost_publication_reply_resumes_without_duplicate_resources_or_reset(self):
        from cloud_gateway import CloudAPIError
        from types import SimpleNamespace
        project='00000000-0000-4000-8000-000000000001'
        config={'project_id':project,'origin_ip':'192.0.2.4','security_group_id':project,
            'container_name':'pc-fixture-edge'}
        setup.save(self.root/'deployment.json',{'mode':'server','host':'192.0.2.4'})
        setup.save(self.root/'installed.json',{'root':'/private/server','memory':{'workspace_id':'fixture'}})
        class Cloud:
            def __init__(self):self.registry=None;self.container=None;self.secret=None;self.calls=[];self.lose=True
            def request(self,service,path,method='GET',body=None,query=None):
                if method!='GET':self.calls.append((service,method,path))
                if service=='registry':
                    if method=='POST':
                        self.registry={'id':'registry-id','name':config['container_name'],'status':'ACTIVE','isPublic':False,'registryType':'DOCKER'}
                        return {'resourceId':'registry-id'}
                    return {'registries':[]} if path=='/v1/registries' else self.registry
                if service=='secrets':
                    if method=='POST':self.secret={'payload':body['payload']};return {}
                    if self.secret is None:raise CloudAPIError(404,method)
                    return self.secret
                if method in ('POST','PATCH'):
                    self.container=json.loads(json.dumps(body));self.container.update(id='container-id',projectId=project,status='running')
                    self.container['configuration']['ingress']['publicUri']='memory.example'
                    if method=='PATCH' and self.lose:self.lose=False;raise ConnectionError('Lost PATCH reply')
                    return {'resourceId':'container-id'}
                if self.container is None:raise CloudAPIError(404,method)
                return self.container
        cloud=Cloud();commands=[]
        def command(args,**kwargs):
            commands.append(args)
            return SimpleNamespace(returncode=0,stdout=('x86_64\n' if args[-1]=='uname -m' else 'sha256:'+'a'*64))
        def remote(state,action,origin,generation=None):
            if action=='configure':return {'runtime':'/private/server/oauth10','origin_service':'pc-gateway-origin.service'}
            if action=='login':return {'private_login':{'code':'Secret123456','login_url':origin+'/login'}}
            return {'workspace_id':'fixture'}
        with ExitStack() as stack:
            def replace(target,**kwargs):return stack.enter_context(patch(target,**kwargs))
            memory=replace('setup.install',return_value={'workspace_id':'fixture'})
            replace('rotation_setup.install',return_value={'schedule':{'scheduled':True}})
            replace('setup.recovery_kit',return_value={'backup_verified':True})
            replace('https_setup.CloudAPI',return_value=cloud)
            replace('https_setup.remote',side_effect=remote)
            replace('https_setup.check_public',return_value={'ready':True})
            replace('https_setup.command_env',return_value={'AUTH_KEY_ID':'test','AUTH_SECRET':'private-key'})
            replace('https_setup.ssh_args',return_value=['ssh','test'])
            replace('https_setup.terraform',return_value=SimpleNamespace(stdout='{}'))
            replace('https_setup.subprocess.run',side_effect=command)
            replace('edge_tools.install',return_value=Path('crane'))
            # private_directory on Windows invokes icacls; isolate that from fake subprocess.
            def directory(path):path=Path(path);path.mkdir(parents=True,exist_ok=True);return path
            replace('https_setup.private_directory',side_effect=directory)
            with self.assertRaises(ConnectionError):setup.install_locked(self.root,config)
            result=setup.install_locked(self.root,config)
            self.assertTrue(result['installed']);self.assertEqual(result['workspace_id'],'fixture')
            original_calls=list(cloud.calls);setup.install_locked(self.root,config)
            self.assertEqual(cloud.calls,original_calls)
            self.assertEqual(len(cloud.calls),4) # registry, container, secret, single PATCH
            self.assertEqual(memory.call_count,3) # package update is not skipped by cloud receipts
            self.assertNotIn('Secret123456',json.dumps(result));self.assertNotIn('private-key',json.dumps(result))


if __name__=='__main__':unittest.main()
