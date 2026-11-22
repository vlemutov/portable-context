import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import cloud_gateway as cloud
import https_setup as https
import setup
import quotas
import network


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)

    def test_contract_specific_estimate_and_unverified_quota(self):
        config = dict(mode='cloudru', project_id='test', flavor='small', zone='zone1', disk_gb=40)
        setup.save(self.state/'deployment.json', config)
        with patch.object(cloud.CloudAPI, 'organization', side_effect=cloud.CloudAPIError(403, 'GET', 'unclassified')), patch.object(cloud.CloudAPI, '__init__', return_value=None):
            report = quotas.check(self.state)
        self.assertEqual((report['status'], report['reason']), ('unknown', 'quota_read_forbidden'))
        review = dict(config, checked_at='2026-11-22', quota_source='owner console', remaining='VM 1',
                      price_source='contract', hourly_rub_including_vat={'vm':'0.49511666', 'disk':'0.6344', 'ipv4':'0.2074'},
                      s3_discrepancy='quota 1 / inventory 0')
        evidence = quotas.owner_estimate(config, review)
        self.assertEqual(evidence['base_720h_rub'], '962.58')
        self.assertEqual(evidence['s3_discrepancy'], review['s3_discrepancy'])
        with self.assertRaises(ValueError):
            quotas.owner_estimate(dict(config, disk_gb=80), review)

    def test_owner_page_uses_recorded_origin_and_hides_expired_code(self):
        import owner_page
        import os
        login = self.state/'owner-login-test.json'
        origin = 'https://test.example'
        setup.save(login, {'login_url':origin + '/login/alternative/code?redirect=/settings/account',
                           'expires_in_minutes':20, 'code':'local-test-only'})
        pointer = {'private_file':str(login)}
        page = owner_page.render(self.state, origin, pointer, 'nonce')
        self.assertIn(b'local-test-only', page)
        with self.assertRaises(ValueError):
            owner_page.render(self.state, 'https://other.example', pointer, 'nonce')
        os.utime(login, (1, 1))
        self.assertNotIn(b'local-test-only', owner_page.render(self.state, origin, pointer, 'nonce'))

    def test_stale_measurement_cannot_start_an_ssh_source_plan(self):
        import ssh_recovery
        from argparse import Namespace
        config = {'mode':'cloudru', 'host':'203.0.113.4'}
        setup.save(self.state/'deployment.json', config)
        (self.state/'terraform').mkdir()
        setup.save(self.state/'terraform/terraform.tfstate', {})
        setup.save(self.state/'ssh-egress.json', {'vm':config['host'], 'config':network.digest(config), 'measured_at':1})
        with patch.object(ssh_recovery, 'configure') as change:
            with self.assertRaisesRegex(ValueError, 'repeat ssh-egress'):
                ssh_recovery.recover(self.state, Namespace(plan_route=False,plan_source=True))
            change.assert_not_called()

    def test_client_oauth_binds_read_scope_state_resource_and_pkce(self):
        import client_check
        import base64
        import hashlib
        import http.client
        import socket
        import threading
        import urllib.parse
        try:
            import httpx2
        except ImportError:
            self.skipTest('Requires the MCP client extra')
        origin = 'https://memory.example'
        with socket.socket() as port_probe:
            port_probe.bind(('127.0.0.1', 0))
            port = port_probe.getsockname()[1]
        callback = f'http://127.0.0.1:{port}/callback'
        responses = []
        auth_params = {}
        def open_browser(url):
            auth_params.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            def visit():
                for state in ['wrong', auth_params['state'][0]]:
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    connection.request('GET', '/callback?' + urllib.parse.urlencode({'code':'local', 'state':state}))
                    response = connection.getresponse()
                    responses.append(response.status)
                    response.read()
                    connection.close()
            workers.append(threading.Thread(target=visit))
            workers[-1].start()
            return True
        workers = []
        with (patch.object(client_check, 'CALLBACK', callback), patch.object(client_check.webbrowser, 'open', open_browser),
              patch.object(httpx2, 'Client') as client_type):
            identity = client_type.return_value.__enter__.return_value
            identity.get.return_value = Mock(status_code=200, json=lambda:dict(issuer=origin,
                    authorization_endpoint=origin+'/authorize', token_endpoint=origin+'/token'))
            identity.post.return_value = Mock(status_code=200, json=lambda:{'access_token':'synthetic'})
            self.assertEqual(client_check.authorize(origin, origin+'/mcp', False), 'synthetic')
            workers[0].join(5)
            self.assertEqual(responses, [400, 200])
            posted = identity.post.call_args.kwargs['data']
            self.assertEqual(posted['redirect_uri'], callback)
            self.assertEqual(posted['resource'], origin+'/mcp')
            self.assertEqual(auth_params['scope'], ['openid memory:read'])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(posted['code_verifier'].encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(auth_params['code_challenge'], [challenge])

    def test_container_absence_is_endpoint_specific_and_does_not_hide_other_499(self):
        reason=cloud.error_reason(b'{"message":"Serverless with name is not found","secret":"do-not-print"}')
        error=cloud.CloudAPIError(499,'GET',reason)
        self.assertTrue(https.container_absent(error))
        self.assertNotIn('do-not-print',str(error))
        self.assertFalse(https.container_absent(cloud.CloudAPIError(499,'GET')))
        self.assertFalse(https.container_absent(cloud.CloudAPIError(403,'GET',reason)))
        self.assertEqual(cloud.error_reason(b'Serverless with name is not found'),reason)
        self.assertEqual(cloud.error_reason(b'{"message":"Serverless with name is not found; token=SECRET"}'),'unclassified')

    def test_http_errors_are_classified_without_echoing_response(self):
        api=cloud.CloudAPI.__new__(cloud.CloudAPI);api.opener=Mock()
        api.opener.open.side_effect=urllib.error.HTTPError('https://example',499,'error',{},
            io.BytesIO(b'{"error":{"message":"Serverless with name is not found"},"token":"SECRET"}'))
        with self.assertRaises(cloud.CloudAPIError) as caught:api._request('https://example','GET',None,{})
        self.assertEqual(caught.exception.reason,'container_name_not_found')
        self.assertNotIn('SECRET',str(caught.exception))

    def test_reads_retry_but_mutations_are_never_repeated(self):
        api=cloud.CloudAPI.__new__(cloud.CloudAPI);api.project_id='project';api.token='SECRET'
        with patch.object(api,'_request',side_effect=[cloud.CloudAPIError(503,'GET'),{}]) as request,patch.object(cloud.time,'sleep'):
            self.assertEqual(api.request('registry','/v1/registries'),{})
            self.assertEqual(request.call_count,2)
        for error in (TimeoutError(),cloud.CloudAPIError(503,'POST')):
            with patch.object(api,'_request',side_effect=error) as request:
                with self.assertRaises(type(error)):api.request('registry','/v1/registries',method='POST',body={})
                self.assertEqual(request.call_count,1)
        with patch.object(api,'_request',side_effect=cloud.CloudAPIError(403,'GET')) as request:
            with self.assertRaises(cloud.CloudAPIError):api.request('registry','/v1/registries')
            self.assertEqual(request.call_count,1)

    def test_visibility_retry_is_only_for_newly_created_resource(self):
        read=Mock(side_effect=[cloud.CloudAPIError(403,'GET'),{'status':'ACTIVE'}])
        with patch.object(https.time,'sleep'):
            self.assertEqual(https.wait_for(read,lambda v:v.get('status')=='ACTIVE',newly_created=True)['status'],'ACTIVE')
        read=Mock(side_effect=cloud.CloudAPIError(403,'GET'))
        with self.assertRaises(cloud.CloudAPIError):https.wait_for(read,lambda _:False)
        self.assertEqual(read.call_count,1)

    def test_create_quota_rejection_can_resume_but_lost_reply_stays_ambiguous(self):
        for error,ambiguous in [(cloud.CloudAPIError(403,'POST','quota_exceeded'),False),
                               (cloud.CloudAPIError(503,'POST','capacity_unavailable'),True),(TimeoutError(),True)]:
            journal=https.Journal(self.state,{});intent={};api=Mock();api.request.side_effect=error
            with self.assertRaises(type(error)):https.create_resource(api,'containers','/v2/containers',{},intent,journal)
            self.assertEqual(bool(intent.get('submitted')),ambiguous)
            api.request.assert_called_once()

    def test_ssh_failure_is_not_architecture_or_cloud_init_failure(self):
        with patch.object(setup.subprocess,'run',return_value=subprocess.CompletedProcess([],255,'','timeout')),patch.object(setup.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'SSH transport unavailable'):setup.wait_cloud_init(['ssh'])
        with patch.object(setup.subprocess,'run',return_value=subprocess.CompletedProcess([],255,'','Permission denied')) as run:
            with self.assertRaisesRegex(ValueError,'authentication'):setup.ssh_read(['ssh'],'uname -m',label='Architecture')
            self.assertEqual(run.call_count,1)
        with patch.object(setup.subprocess,'run',side_effect=[subprocess.TimeoutExpired('ssh SECRET',30),
                subprocess.CompletedProcess([],0,'x86_64','')]),patch.object(setup.time,'sleep'):
            self.assertEqual(setup.ssh_read(['ssh'],'uname -m',label='Architecture').stdout,'x86_64')

    def test_resume_requires_live_identity_matching_release_and_fingerprint(self):
        setup.save(self.state/'deployment.json',{})
        setup.save(self.state/'installed.json',{'root':'/owner','release':'release','memory':{'workspace_id':'memory'}})
        setup.save(self.state/'memory-install-checkpoint.json',{'fingerprint':'current',
            'installed_sha256':setup.sha(self.state/'installed.json'),'result':{'workspace_id':'memory'}})
        with patch.object(setup,'install_fingerprint',return_value='current'),patch.object(setup,'ssh_args',return_value=['ssh']),patch.object(setup,'ssh_read') as read:
            read.return_value=subprocess.CompletedProcess([],0,json.dumps({'release':'release','workspace_id':'memory'}))
            self.assertEqual(setup.resume_memory(self.state),{'workspace_id':'memory'})
            read.return_value.stdout=json.dumps({'release':'release','workspace_id':'OTHER'})
            with self.assertRaisesRegex(ValueError,'identity/release changed'):setup.resume_memory(self.state)
        with patch.object(setup,'install_fingerprint',return_value='updated'),patch.object(setup,'ssh_read') as read:
            self.assertIsNone(setup.resume_memory(self.state));read.assert_not_called()

    def test_recovery_acceptance_is_bound_to_boot_status_and_configuration(self):
        setup.save(self.state/'deployment.json',{'host':'example'})
        status={'status':'done','errors':['old package failure']}
        result=subprocess.CompletedProcess([],2,json.dumps(status))
        with patch.object(setup,'ssh_args',return_value=['ssh']),patch.object(setup,'ssh_read',return_value=result),patch.object(setup,'server_readiness',return_value={'boot_id':'boot'}):
            with self.assertRaisesRegex(ValueError,'Cloud-init failed'):setup.wait_cloud_init(['ssh'],self.state)
            setup.accept_cloud_init_recovery(self.state)
            setup.wait_cloud_init(['ssh'],self.state)
        with patch.object(setup,'ssh_read',return_value=result),patch.object(setup,'server_readiness',return_value={'boot_id':'new-boot'}):
            with self.assertRaisesRegex(ValueError,'Cloud-init failed'):setup.wait_cloud_init(['ssh'],self.state)

    def test_status_and_journal_do_not_include_arbitrary_exception_text(self):
        setup.save(self.state/'deployment.json',{'name':'test'})
        journal=https.Journal(self.state,{})
        with self.assertRaises(RuntimeError):journal.stage('image',Mock(side_effect=RuntimeError('SECRET')))
        report=https.installation_status(self.state)
        self.assertNotIn('SECRET',json.dumps(report));self.assertEqual(report['last_error']['stage'],'image')

    def test_registry_host_discovery_is_scoped_to_saved_project(self):
        setup.save(self.state/'deployment.json',{'mode':'cloudru','project_id':'one'})
        setup.save(self.state/'https-journal.json',{'config':{'project_id':'one'},'completed':{'registry':{'name':'pc-new-edge'}}})
        self.assertEqual(network.registry_hosts(self.state),['pc-new-edge.cr.cloud.ru'])
        setup.save(self.state/'deployment.json',{'mode':'cloudru','project_id':'two'})
        with self.assertRaises(ValueError):network.registry_hosts(self.state)

    def test_quota_comparison_includes_parent_usage_and_unknown_is_not_success(self):
        scopes=[('PROJECT','p'),('CUSTOMER','c')]
        rows=[{'resource_type_code':'cpu','subject_type_code':scope,'subject_id':identifier,
               'individual_limit':'10','aggregated_usage':used} for (scope,identifier),used in zip(scopes,['1','9'])]
        self.assertEqual(quotas.evaluate(rows,{'cpu':2},scopes)[0]['status'],'insufficient')
        self.assertEqual(quotas.evaluate(rows,{'cpu':1},scopes)[0]['status'],'sufficient')
        self.assertEqual(quotas.evaluate(rows[:1],{'cpu':1},scopes)[0]['status'],'unknown')
        rows[1]['individual_limit']='-1'
        self.assertEqual(quotas.evaluate(rows,{'cpu':1},scopes)[0]['status'],'unknown')
        with self.assertRaises(ValueError):quotas.evaluate(rows,{'cpu':'NaN'},scopes)

    def test_quota_pagination_and_access_denial(self):
        api=Mock();api.organization.side_effect=[{'usages':[{'id':1}],'next_page_token':'two'},{'usages':[{'id':2}]}]
        self.assertEqual(len(quotas.pages(api,'/v3/quotas/usages','usages',{})),2)
        self.assertEqual(api.organization.call_args.args[1]['page_token'],'two')
        setup.save(self.state/'deployment.json',{'mode':'cloudru','project_id':'p'})
        with patch.object(quotas,'CloudAPI',side_effect=cloud.CloudAPIError(403,'POST')):
            result=quotas.check(self.state)
        self.assertEqual(result['status'],'unknown');self.assertEqual(result['http_status'],403)

    def test_reviewed_quota_shortage_blocks_only_matching_plan_and_remaining_resources(self):
        setup.save(self.state/'deployment.json',{'mode':'cloudru','project_id':'p'})
        setup.save(self.state/'https-plan.json',{'config':{'project_id':'p'}})
        with patch.object(quotas,'check',return_value={'status':'insufficient'}) as check:
            quotas.review(self.state,{'real-api-code':1},'https')
            journal=https.Journal(self.state,{'project_id':'p'})
            journal.value['stage']='memory';journal.save()
            with self.assertRaisesRegex(ValueError,'Insufficient quota'):quotas.preflight(self.state,'https')
            self.assertEqual(check.call_args.args[1],{'real-api-code':1})
            journal.value['completed']['registry']={'id':'created'};journal.save()
            check.return_value={'status':'review_required'}
            quotas.preflight(self.state,'https')
            self.assertIsNone(check.call_args.args[1])

    def test_readiness_rejects_clock_skew_and_missing_dependency(self):
        healthy={'utc_epoch':1000,'python_ok':True,'venv':True,'ensurepip':True,'cron':True,'boot_id':'boot'}
        with patch.object(setup.time,'time',return_value=1000),patch.object(setup,'ssh_read') as read:
            read.return_value=subprocess.CompletedProcess([],0,json.dumps(healthy))
            self.assertEqual(setup.server_readiness(['ssh'])['boot_id'],'boot')
            read.return_value.stdout=json.dumps({**healthy,'utc_epoch':0})
            with self.assertRaisesRegex(ValueError,'clock'):setup.server_readiness(['ssh'])
            read.return_value.stdout=json.dumps({**healthy,'cron':False})
            with self.assertRaisesRegex(ValueError,'dependencies'):setup.server_readiness(['ssh'])

    def test_network_inspection_reports_dns_failure_but_route_plan_refuses_it(self):
        with patch.object(network.socket,'getaddrinfo',side_effect=network.socket.gaierror()):
            endpoints=network.targets({'mode':'server','host':'vm.example'},'server',tolerate_dns=True)
            self.assertEqual(endpoints[0]['dns'],'failed')
            with self.assertRaisesRegex(ValueError,'DNS'):network.targets({'mode':'server','host':'vm.example'},'server')


if __name__=='__main__':unittest.main()
