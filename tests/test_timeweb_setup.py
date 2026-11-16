import copy
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
import dns_ready


class FakeAPI:
    def __init__(self):
        self.items={};self.posts=[];self.fail=None;self.counter=100
        self.catalogs={'/api/v1/presets/servers':{'server_presets':[{'id':1,'price':500,'location':'ru-1','ram':2048,'disk':40960}]},
            '/api/v1/presets/storages':{'storages_presets':[{'id':2,'price':10,'location':'ru-1'}]},
            '/api/v1/presets/apps':{'backend_presets':[{'id':3,'price':510,'location':'ru-1'}]},
            '/api/v1/os/servers':{'servers_os':[{'id':4,'name':'ubuntu','version':'24.04'}]}}
    def request(self,path,method='GET',body=None):
        if path in self.catalogs:return copy.deepcopy(self.catalogs[path])
        canonical=path.replace('/api/v2/apps','/api/v1/apps')
        keys={'/api/v1/projects':'project','/api/v1/ssh-keys':'ssh-key','/api/v1/servers':'server',
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
            'source_branch':'main','source_commit':'a'*40,'dns_timeout':1800}
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

    def test_retry_deploy_needs_exact_review_and_never_runs_while_pending(self):
        setup.save(self.state/'timeweb-journal.json',{'config':self.config,'resources':{'app':{'id':123}}})
        setup.save(self.state/'timeweb-publish.json',{'desired':'digest','submitted':True,'baseline_deploys':[]})
        api=Mock()
        app={'app':{'id':123,'name':'pc-test-edge'}}
        running={'deploys':[{'id':'d','status':'prepare','commit_sha':'a'*40}]}
        api.request.side_effect=[app,running]
        with patch('timeweb_setup.API',return_value=api),self.assertRaisesRegex(ValueError,'pending'):tw.retry_deploy(self.state)
        failed={'deploys':[{'id':'d','status':'failure','commit_sha':'a'*40}]}
        api.request.side_effect=[app,failed,app,failed,app,failed]
        with patch('timeweb_setup.API',return_value=api):
            preview=tw.retry_deploy(self.state)
            with self.assertRaises(ValueError):tw.retry_deploy(self.state,'wrong')
            tw.retry_deploy(self.state,preview['plan_sha256'])
        self.assertFalse(setup.read(self.state/'timeweb-publish.json')['submitted'])
        self.assertTrue(all('method' not in c.kwargs for c in api.request.call_args_list))

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
