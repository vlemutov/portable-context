import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import setup
import timeweb_setup as tw
import timeweb_api
import timeweb_selection as selection
import dns_ready


class FakeAPI:
    def __init__(self):
        self.items={};self.posts=[];self.fail=None;self.counter=100
        self.catalogs={'/api/v1/presets/servers':{'server_presets':[{'id':1,'price':500,'location':'ru-1','ram':2048,'disk':40960}]},
            '/api/v1/presets/storages':{'storages_presets':[{'id':2,'price':10,'location':'ru-1'}]},
            '/api/v1/presets/apps':{'backend_presets':[{'id':3,'price':510,'location':'ru-1'}]},
            '/api/v1/os/servers':{'servers_os':[{'id':4,'name':'ubuntu','version':'24.04'}]},
            '/api/v2/locations':{'locations':[{'location':'ru-1','availability_zones':['spb-3']}]} }
    def request(self,path,method='GET',body=None):
        if path in self.catalogs:return copy.deepcopy(self.catalogs[path])
        canonical=path.replace('/api/v2/apps','/api/v1/apps')
        keys={'/api/v1/projects':'project','/api/v1/ssh-keys':'ssh_key','/api/v1/servers':'server',
              '/api/v1/storages/buckets':'bucket','/api/v1/vcs-provider':'provider','/api/v1/apps':'app'}
        if method=='POST':
            self.posts.append((path,copy.deepcopy(body)));self.counter+=1
            ident=self.counter;key=keys[canonical];value={**body,'id':ident}
            if key=='provider':
                value={'provider_id':ident}
                response={'provider':value,'repositories':[{'id':'repo','url':body['url']}]}
            elif key=='server':
                value.pop('project_id');value['networks']=[{'type':'public','ips':[{'ip':'8.8.4.4','type':'ipv4'}]}]
                response={key:value}
            elif key=='bucket':response={key:{**value,'hostname':'s3.twcstorage.ru','access_key':'secret-access','secret_key':'secret-key'}}
            elif key=='app':response={key:{**value,'status':'active','ip':'8.8.8.8','domains':[{'fqdn':'test.twc1.net','is_technical':True}]}}
            else:response={key:value}
            self.items[canonical+'/'+str(ident)]=response
            if self.fail==path:raise timeweb_api.TimewebError(0,'POST')
            return copy.deepcopy(response)
        if method=='GET':return copy.deepcopy(self.items[canonical])
        raise AssertionError(method+' '+path)


class TimewebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name).resolve();self.api=FakeAPI()
        self.config={'format':1,'mode':'timeweb','name':'pc-test','ssh_user':'pcadmin','ssh_port':22,
            'bucket':'pc-test-backups','server_preset_id':1,'storage_preset_id':2,'app_preset_id':3,'os_id':4,
            'ssh_cidr':'203.0.113.1/32','source_url':'https://github.com/example/memory.git',
            'source_branch':'main','source_commit':'a'*40,'dns_timeout':1800,'availability_zone':'spb-3'}
        setup.save(self.state/'deployment.json',self.config)
        (self.state/'id_ed25519').write_text('private-test-key')
        (self.state/'id_ed25519.pub').write_text('ssh-ed25519 synthetic')
        self.patch=patch('timeweb_setup.API',return_value=self.api);self.patch.start();self.addCleanup(self.patch.stop)

    def plan(self):return tw.plan(self.state)

    def test_approved_plan_bound_to_configuration_and_key(self):
        plan=self.plan()
        with self.assertRaises(ValueError):tw.apply(self.state,'wrong')
        self.assertEqual(self.api.posts,[])
        tw.approved(self.state,plan['plan_sha256'])
        c=setup.read(self.state/'deployment.json');c['host']='8.8.4.4';setup.save(self.state/'deployment.json',c)
        tw.approved(self.state)
        (self.state/'id_ed25519.pub').write_text('other')
        with self.assertRaisesRegex(ValueError,'SSH key'):tw.approved(self.state)

    def test_infrastructure_resume_reuses_all_ids_and_keeps_secrets_private(self):
        plan=self.plan();first=tw.apply(self.state,plan['plan_sha256']);count=len(self.api.posts)
        second=tw.apply(self.state)
        self.assertEqual(first,second);self.assertEqual(len(self.api.posts),count)
        self.assertEqual(count,4)
        self.assertEqual(setup.read(self.state/'backup-config.json')['secret_key'],'secret-key')
        self.assertNotIn('secret-key',json.dumps(tw.status(self.state)))
        self.assertNotIn('secret-key',(self.state/'timeweb-journal.json').read_text())

    def test_lost_creation_reply_does_not_create_a_second_server(self):
        plan=self.plan();self.api.fail='/api/v1/servers'
        with self.assertRaises(timeweb_api.TimewebError):tw.apply(self.state,plan['plan_sha256'])
        self.api.fail=None
        with self.assertRaisesRegex(ValueError,'outcome unknown'):tw.apply(self.state)
        self.assertEqual(sum(p=='/api/v1/servers' for p,_ in self.api.posts),1)
        ident=next(v['server']['id'] for v in self.api.items.values() if 'server' in v)
        tw.reconcile(self.state,'server',str(ident));tw.apply(self.state)
        self.assertEqual(sum(p=='/api/v1/servers' for p,_ in self.api.posts),1)

    def test_reconciled_foreign_vm_rejected_before_secrets_sent(self):
        plan=self.plan();self.api.fail='/api/v1/servers'
        with self.assertRaises(timeweb_api.TimewebError):tw.apply(self.state,plan['plan_sha256'])
        ident=next(v['server']['id'] for v in self.api.items.values() if 'server' in v)
        self.api.items['/api/v1/servers/'+str(ident)]['server']['cloud_init']='another memory'
        tw.reconcile(self.state,'server',str(ident))
        with self.assertRaisesRegex(ValueError,'identity'):tw.apply(self.state)
        self.assertFalse((self.state/'backup-config.json').exists())

    def test_public_bucket_and_foreign_region_are_rejected(self):
        self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]['location']='nl-1'
        with self.assertRaisesRegex(ValueError,'Russian'):self.plan()
        self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]['location']='ru-1'
        plan=self.plan();tw.apply(self.state,plan['plan_sha256'])
        for row in self.api.items.values():
            if 'bucket' in row:row['bucket']['type']='public'
        with self.assertRaisesRegex(ValueError,'identity'):tw.apply(self.state)

    def test_configuration_isolation_and_cloud_init_firewall(self):
        self.plan()
        with self.assertRaisesRegex(ValueError,'another installation'):setup.configure(self.state,{'source_commit':'b'*40})
        cloud=json.loads(tw.cloud_init(self.config,'public-key').split('\n',1)[1])
        self.assertFalse(cloud['ssh_pwauth'])
        self.assertIn(['ufw','allow','from','203.0.113.1/32','to','any','port','22','proto','tcp'],cloud['runcmd'])
        self.assertIn(['ufw','allow','9443/tcp'],cloud['runcmd'])

    def test_zone_must_match_preset_and_is_sent_in_server_request(self):
        config={**self.config,'availability_zone':'msk-1'};setup.save(self.state/'deployment.json',config)
        with self.assertRaisesRegex(ValueError,'does not match'):self.plan()
        self.assertEqual(self.api.posts,[])
        setup.save(self.state/'deployment.json',self.config)
        plan=self.plan();tw.apply(self.state,plan['plan_sha256'])
        body=next(v for p,v in self.api.posts if p=='/api/v1/servers')
        self.assertEqual(body['availability_zone'],'spb-3')

    def test_masked_credentials_are_actionable_and_not_saved(self):
        plan=self.plan();tw.apply(self.state,plan['plan_sha256'])
        vm=next(v['server'] for v in self.api.items.values() if 'server' in v)
        vm['cloud_init']='hidden-by-api-key-policy'
        with self.assertRaisesRegex(ValueError,'token masks secrets'):tw.apply(self.state)
        vm['cloud_init']=tw.cloud_init(self.config,'ssh-ed25519 synthetic')
        bucket=next(v['bucket'] for v in self.api.items.values() if 'bucket' in v)
        bucket['secret_key']='hidden-by-api-key-policy'
        before=(self.state/'backup-config.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'hidden by token'):tw.apply(self.state)
        self.assertEqual(before,(self.state/'backup-config.json').read_bytes())

    def test_ipv4_lost_response_never_purchases_twice(self):
        j=tw.Journal(self.state,self.config)
        vm={'id':123,'networks':[{'type':'public','ips':[]}]}
        api=Mock();api.request.side_effect=timeweb_api.TimewebError(0,'POST')
        with self.assertRaises(timeweb_api.TimewebError):tw.ensure_server_ipv4(api,j,vm)
        with self.assertRaisesRegex(ValueError,'pending or unknown'):tw.ensure_server_ipv4(api,j,vm)
        self.assertEqual(api.request.call_count,1)
        vm['networks'][0]['ips']=[{'type':'ipv4','ip':'8.8.4.4'}]
        self.assertEqual(tw.ensure_server_ipv4(api,j,vm),'8.8.4.4')
        self.assertEqual(api.request.call_count,1)

    def test_dependencies_prepared_only_when_missing(self):
        ready={'python_ok':True,'venv':True,'ensurepip':True,'cron':True,'package_setup_running':False}
        with patch('setup.ssh_read',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(ready))) as ssh:
            setup.prepare_timeweb_dependencies(['ssh']);self.assertEqual(ssh.call_count,1)
        missing={**ready,'ensurepip':False}
        with patch('setup.ssh_read',side_effect=[SimpleNamespace(returncode=0,stdout=json.dumps(missing)),SimpleNamespace(returncode=0)]) as ssh:
            setup.prepare_timeweb_dependencies(['ssh'])
            self.assertIn('DPkg::Lock::Timeout=180',ssh.call_args.args[1])
            self.assertEqual(ssh.call_args.kwargs['attempts'],1)
    def fallback_fixture(self):
        source=self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]
        source.update({'cpu':1,'cpu_frequency':'3.3','disk_type':'nvme','tags':['msk_nvme']})
        self.api.catalogs['/api/v1/presets/servers']['server_presets'].append({**source,'id':5,'price':800,'tags':['ultra_msk_nvme']})
        self.plan()
        j=tw.Journal(self.state,self.config)
        j.value['resources']={'project':{'id':55},'ssh_key':{'id':56},'server':{'submitted':False}}
        j.value['last_error']={'stage':'infrastructure','type':'TimewebError','http_status':409,'provider_error_code':'no_free_node'}
        j.save();self.api.items['/api/v1/servers']={'servers':[]}
        api_patch=patch('timeweb_selection.API',return_value=self.api);api_patch.start();self.addCleanup(api_patch.stop)

    def test_fallback_price_review_preserves_project_and_key(self):
        self.fallback_fixture()
        self.assertEqual(len(selection.options(self.state)['options']),1)
        options=selection.options(self.state,max_price=800)['options']
        self.assertEqual(options[1]['family'],'ultra');self.assertEqual(options[1]['capacity'],'unknown')
        preview=selection.select(self.state,5,'spb-3')
        self.assertEqual(preview['monthly_vm_price_delta'],300)
        self.assertEqual(setup.read(self.state/'deployment.json')['server_preset_id'],1)
        with self.assertRaises(ValueError):selection.select(self.state,5,'spb-3','wrong')
        selection.select(self.state,5,'spb-3',preview['plan_sha256'])
        j=setup.read(self.state/'timeweb-journal.json')
        self.assertEqual(j['resources'],{'project':{'id':55},'ssh_key':{'id':56}})
        self.assertEqual(j['config']['server_preset_id'],5);self.assertEqual(self.api.posts,[])
        with self.assertRaises(ValueError):tw.approved(self.state)

    def test_fallback_blocks_ambiguous_or_existing_vm_and_price_changes(self):
        self.fallback_fixture();path=self.state/'timeweb-journal.json';j=setup.read(path)
        j['last_error']['http_status']=0;setup.save(path,j)
        with self.assertRaisesRegex(ValueError,'definitive'):selection.select(self.state,5,'spb-3')
        j['last_error']['http_status']=409;setup.save(path,j)
        self.api.items['/api/v1/servers']['servers']=[{'id':88,'name':self.config['name']}]
        with self.assertRaisesRegex(ValueError,'Matching VM'):selection.select(self.state,5,'spb-3')
        self.api.items['/api/v1/servers']['servers']=[]
        preview=selection.select(self.state,5,'spb-3')
        self.api.catalogs['/api/v1/presets/servers']['server_presets'][1]['price']=900
        with self.assertRaisesRegex(ValueError,'preview changed'):selection.select(self.state,5,'spb-3',preview['plan_sha256'])
        self.assertEqual(self.api.posts,[])

    def test_fallback_resumes_interrupted_local_transition(self):
        self.fallback_fixture();preview=selection.select(self.state,5,'spb-3');real=setup.save
        def fail(path,value):
            if path.name=='timeweb-journal.json':raise OSError('interrupted')
            return real(path,value)
        with patch('timeweb_selection.save',side_effect=fail):
            with self.assertRaises(OSError):selection.select(self.state,5,'spb-3',preview['plan_sha256'])
        with self.assertRaisesRegex(ValueError,'interrupted'):tw.approved(self.state)
        selection.select(self.state,5,'spb-3',preview['plan_sha256'])
        self.assertTrue(setup.read(self.state/'timeweb-vm-selection.json')['complete'])
        self.assertEqual(setup.read(self.state/'deployment.json')['server_preset_id'],5)

    def test_dns_wait_resets_streak_and_resumes_without_cached_success(self):
        ticks=[0];sleep=lambda seconds:ticks.__setitem__(0,ticks[0]+seconds)
        ready={'dns':True,'https':True,'reason':'ready'};bad={'dns':False,'https':False,'reason':'dns_pending'}
        probe=Mock(side_effect=[ready,bad,ready,ready,ready])
        value=dns_ready.wait(self.state,'https://test.twc1.net','8.8.8.8',timeout=100,interval=10,probe=probe,clock=lambda:ticks[0],sleep=sleep)
        self.assertTrue(value['ready']);self.assertEqual(probe.call_count,5)
        with self.assertRaisesRegex(ValueError,'Resources retained'):
            dns_ready.wait(self.state,'https://test.twc1.net','8.8.8.8',timeout=1,interval=1,probe=Mock(return_value=bad),clock=lambda:ticks[0],sleep=sleep)
        self.assertFalse(setup.read(self.state/'dns-readiness.json')['ready'])

    def test_dns_success_does_not_hide_https_failure(self):
        ticks=[0]
        with self.assertRaises(ValueError):dns_ready.wait(self.state,'https://test.twc1.net','8.8.8.8',timeout=1,interval=1,
            probe=lambda *_:{'dns':True,'https':False},clock=lambda:ticks[0],sleep=lambda s:ticks.__setitem__(0,ticks[0]+s))

    def test_dns_gate_fails_then_same_installation_finishes(self):
        plan=self.plan()
        architecture=patch('setup.ssh_read',return_value=SimpleNamespace(returncode=0,stdout='x86_64\n'))
        architecture.start();self.addCleanup(architecture.stop)
        ssh=patch('setup.ssh_args',return_value=['ssh']);ssh.start();self.addCleanup(ssh.stop)
        def fake_generation(path,host):
            path.mkdir();[(path/name).write_text('synthetic') for name in ('edge.pem','edge-key.pem','origin-ca.pem')]
        def rotation(state,options,generation):
            setup.save(state/'rotation-installed.json',{'schedule':'timer'});return {'schedule':'timer'}
        with patch('setup.resume_memory',return_value={'workspace_id':'same-memory'}), \
             patch('dns_ready.wait',side_effect=ValueError('DNS timeout')):
            with self.assertRaisesRegex(ValueError,'DNS timeout'):tw.install(self.state,plan['plan_sha256'])
        count=len(self.api.posts);app_id=setup.read(self.state/'timeweb-journal.json')['resources']['app']['id']
        self.assertFalse((self.state/'https-installed.json').exists())
        with patch('setup.resume_memory',return_value={'workspace_id':'same-memory'}),patch('dns_ready.wait',return_value={'ready':True}), \
             patch('gateway.issue_credentials',side_effect=fake_generation),patch('https_setup.remote',return_value={'origin_service':'pc-origin.service'}), \
             patch('timeweb_setup.publish',return_value=True),patch('timeweb_rotation.install',side_effect=rotation), \
             patch('https_setup.check_public',return_value={'ready':True}),patch('setup.recovery_kit',return_value={'verified':True}), \
             patch('https_setup.owner_login',return_value={'private_file':'private'}):
            result=tw.install(self.state)
        self.assertEqual(len(self.api.posts),count)
        self.assertEqual(setup.read(self.state/'timeweb-journal.json')['resources']['app']['id'],app_id)
        self.assertEqual(result['workspace_id'],'same-memory');self.assertEqual(result['mcp_url'],'https://test.twc1.net/mcp')

    def test_publication_lost_reply_is_reconciled_without_redeploy(self):
        api=Mock();app={'id':1,'envs':{'K':'V'}};path=self.state/'publish.json'
        api.request.side_effect=[{'deploys':[]},timeweb_api.TimewebError(0,'POST')]
        with self.assertRaises(timeweb_api.TimewebError):tw.publish(api,app,{'K':'V'},'a'*40,path)
        api.reset_mock();api.request.side_effect=None
        api.request.return_value={'deploys':[{'id':'d1','commit_sha':'a'*40,'status':'success'}]}
        self.assertTrue(tw.publish(api,app,{'K':'V'},'a'*40,path))
        self.assertEqual(api.request.call_count,1)
        self.assertNotIn('method',api.request.call_args.kwargs)

    def test_token_never_follows_redirect_and_mutations_never_retry(self):
        setup.save(self.state/'credentials.json',{'TIMEWEB_TOKEN':'secret-token'})
        api=timeweb_api.API(self.state)
        error=urllib.error.HTTPError('https://api.timeweb.cloud/api/v1/projects',302,'redirect',{},None)
        with patch.object(api.opener,'open',side_effect=error) as request:
            with self.assertRaises(timeweb_api.TimewebError) as caught:api.request('/api/v1/projects',method='POST',body={'name':'test'})
        self.assertEqual(request.call_count,1);self.assertNotIn('secret-token',str(caught.exception))
        with self.assertRaises(ValueError):api.request('//attacker.test')

    def test_provider_error_code_survives_without_secret_message_or_post_retry(self):
        setup.save(self.state/'credentials.json',{'TIMEWEB_TOKEN':'secret-token'})
        api=timeweb_api.API(self.state)
        body=json.dumps({'error_code':'no_free_node','message':'secret-token and private details'}).encode()
        error=urllib.error.HTTPError('https://api.timeweb.cloud/api/v1/servers',409,'Conflict',{},io.BytesIO(body))
        with patch.object(api.opener,'open',side_effect=error) as request:
            with self.assertRaises(timeweb_api.TimewebError) as caught:api.request('/api/v1/servers',method='POST',body={})
        self.assertEqual(request.call_count,1)
        self.assertEqual(caught.exception.error_code,'no_free_node')
        self.assertNotIn('secret-token',str(caught.exception))

    def test_capacity_rejection_is_retryable_but_unknown_conflict_is_not(self):
        for code,unresolved in [('no_free_node',False),(None,True)]:
            with self.subTest(code=code):
                folder=self.state/('capacity' if code else 'unknown');folder.mkdir()
                journal=tw.Journal(folder,self.config);api=Mock()
                api.request.side_effect=timeweb_api.TimewebError(409,'POST',code)
                with self.assertRaises(timeweb_api.TimewebError):
                    journal.resource(api,'server','/api/v1/servers','server',{'name':'test'},lambda v:True)
                self.assertEqual(journal.value['resources']['server']['submitted'],unresolved)
                self.assertEqual(api.request.call_count,1)

    def test_retry_deploy_needs_exact_review_and_never_runs_while_pending(self):
        setup.save(self.state/'timeweb-journal.json',{'config':self.config,'resources':{'app':{'id':123}}})
        setup.save(self.state/'timeweb-publish.json',{'desired':'digest','submitted':True,'baseline_deploys':[]})
        api=Mock()
        app={'app':{'id':123,'name':'pc-test-edge','commit_sha':'a'*40}}
        running={'deploys':[{'id':'d','status':'prepare','commit_sha':'a'*40}]}
        api.request.side_effect=[app,running]
        with patch('timeweb_setup.API',return_value=api),self.assertRaisesRegex(ValueError,'pending'):tw.retry_deploy(self.state)
        failed={'deploys':[{'id':'d','status':'failure','commit_sha':'a'*40},{'id':'superseded','status':'stopped','commit_sha':'a'*40}]}
        api.request.side_effect=[app,failed,app,failed,app,failed]
        with patch('timeweb_setup.API',return_value=api):
            preview=tw.retry_deploy(self.state)
            with self.assertRaises(ValueError):tw.retry_deploy(self.state,'wrong')
            tw.retry_deploy(self.state,preview['plan_sha256'])
        self.assertFalse(setup.read(self.state/'timeweb-publish.json')['submitted'])
        self.assertTrue(all('method' not in c.kwargs for c in api.request.call_args_list))

    def test_initial_failed_build_can_retry_before_tls_publication_exists(self):
        setup.save(self.state/'timeweb-journal.json',{'config':self.config,'resources':{'app':{'id':123}}})
        api=Mock();app={'id':123,'name':'pc-test-edge','commit_sha':'a'*40,'envs':{}}
        failed={'deploys':[{'id':'first','status':'failure','commit_sha':'a'*40}]}
        api.request.side_effect=[{'app':app},failed,{'app':app},failed]
        path=self.state/'timeweb-bootstrap-publish.json'
        with patch('timeweb_setup.API',return_value=api):
            preview=tw.retry_deploy(self.state)
            self.assertFalse(path.exists())
            tw.retry_deploy(self.state,preview['plan_sha256'])
        self.assertFalse((self.state/'timeweb-publish.json').exists())
        api.reset_mock();api.request.side_effect=timeweb_api.TimewebError(0,'POST')
        with self.assertRaises(timeweb_api.TimewebError):tw.publish(api,app,{},'a'*40,path)
        api.reset_mock();api.request.side_effect=None
        api.request.return_value={'deploys':[{'id':'retry','status':'success','commit_sha':'a'*40}]}
        self.assertTrue(tw.publish(api,app,{},'a'*40,path))
        self.assertEqual(api.request.call_count,1)
        self.assertNotIn('method',api.request.call_args.kwargs)

    def test_renewal_reconciles_existing_deploy_and_keeps_edge_identity(self):
        from gateway import issue_credentials
        from timeweb_rotation import TimewebBackend
        keys=self.state/'keys';issue_credentials(keys,'test.twc1.net')
        backend=TimewebBackend.__new__(TimewebBackend)
        backend.state=self.state;backend.config={'app_id':123,'app_name':'pc-test-edge','public_host':'test.twc1.net',
            'source_commit':'a'*40,'origin_ip':'8.8.4.4'}
        app={'id':123,'name':'pc-test-edge','commit_sha':'a'*40,'domains':[{'fqdn':'test.twc1.net','is_technical':True}]}
        backend.api=Mock();backend.api.request.return_value={'app':app};backend.public_probe=Mock(return_value=True)
        with patch('timeweb_rotation.publish',return_value=True) as publish:
            self.assertTrue(backend.edge(keys,[keys],'new-edge_switch',{'new':'new'}))
            self.assertEqual(publish.call_args.args[3],'a'*40)
        backend.api.request.return_value={'app':{**app,'name':'another-memory'}}
        with self.assertRaisesRegex(ValueError,'identity'):backend.container()


if __name__=='__main__':unittest.main()
