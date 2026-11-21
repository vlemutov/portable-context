import copy
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import setup
import timeweb_auto as auto
import timeweb_setup as tw
import timeweb_selection as selection
from timeweb_api import TimewebError
import test_timeweb_setup as fixtures


class AutoTests(unittest.TestCase):
    def setUp(self):
        fixtures.TimewebTests.setUp(self)
        row=self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]
        row.update(cpu=1,disk_type='nvme',tags=['msk_nvme'])
        self.api.catalogs['/api/v1/presets/servers']['server_presets'] += [
            {**row,'id':5,'price':700,'tags':['ultra_msk_nvme']},
            {**row,'id':6,'price':800,'location':'ru-3'}]
        self.api.catalogs['/api/v2/locations']['locations'].append({'location':'ru-3','availability_zones':['msk-1']})
        tw.plan(self.state)
        for module in ('timeweb_auto','timeweb_selection'):
            p=patch(module+'.API',return_value=self.api);p.start();self.addCleanup(p.stop)
        self.outcomes={};self.created=[];self.now=1000
        self.clock=patch('timeweb_auto.time.time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        sleeper=patch('timeweb_auto.time.sleep',side_effect=lambda seconds:setattr(self,'now',self.now+seconds))
        sleeper.start();self.addCleanup(sleeper.stop)
        real=self.api.request
        def request(path,method='GET',body=None):
            if path=='/api/v1/servers' and method=='GET':
                return {'servers':[v['server'] for k,v in self.api.items.items() if k.startswith('/api/v1/servers/')],
                        'meta':{'total':len([k for k in self.api.items if k.startswith('/api/v1/servers/')])}}
            if path=='/api/v1/servers' and method=='POST':
                self.created.append(body['preset_id'])
                outcome=self.outcomes.get(body['preset_id'])
                if outcome:raise outcome
            return real(path,method=method,body=body)
        self.api.request=request

    def prepare(self,**kwargs):
        return auto.prepare(self.state,kwargs.pop('locations',['ru-1','ru-3']),
                            kwargs.pop('total',2000),kwargs.pop('extras',200),**kwargs)

    def run_plan(self,review):
        return auto.execute(self.state,review['plan_sha256'],True)

    def test_preview_prices_order_and_no_cloud_mutations(self):
        review=self.prepare(locations=['ru-3','ru-1'],max_attempts=2)
        self.assertEqual([r['preset_id'] for r in review['candidates']],[6,1])
        self.assertEqual(review['candidates'][0]['estimated_monthly_total'],1520)
        self.assertEqual(self.api.posts,[])
        self.assertEqual(setup.read(self.state/'deployment.json'),self.config)
        self.assertEqual([r['preset_id'] for r in self.prepare(families=['ultra'])['candidates']],[5])
        self.assertEqual([r['zone'] for r in self.prepare(zones=['msk-1'])['candidates']],['msk-1'])
        for kwargs in ({'total':100},{'total':float('inf')},{'extras':-1},{'locations':['nl-1']},{'max_attempts':21},{'timeout':29}):
            with self.assertRaises(ValueError):self.prepare(**kwargs)

    def test_review_and_payment_required_and_stale_state_blocks(self):
        review=self.prepare()
        with self.assertRaises(ValueError):auto.execute(self.state,'wrong',True)
        with self.assertRaisesRegex(ValueError,'payment-reviewed'):auto.execute(self.state,review['plan_sha256'])
        setup.save(self.state/'deployment.json',{**self.config,'name':'changed'})
        with self.assertRaisesRegex(ValueError,'changed'):self.run_plan(review)
        self.assertEqual(self.api.posts,[])

    def test_capacity_failure_then_one_success_preserves_project_key_and_stops(self):
        self.outcomes[1]=TimewebError(409,'POST','no_free_node')
        review=self.prepare()
        result=self.run_plan(review)
        self.assertEqual(result['status'],'selected')
        self.assertEqual(self.created,[1,5])
        journal=setup.read(self.state/'timeweb-journal.json')
        self.assertEqual(set(journal['resources']),{'project','ssh_key','server'})
        self.assertEqual([p for p,b in self.api.posts].count('/api/v1/projects'),1)
        self.assertEqual([p for p,b in self.api.posts].count('/api/v1/ssh-keys'),1)
        self.assertFalse(any('storages' in p or '/ips' in p or 'apps' in p for p,b in self.api.posts))
        self.assertEqual(result['attempts'][0]['http_status'],409)
        self.run_plan(review)
        self.assertEqual(self.created,[1,5])
        with self.assertRaisesRegex(ValueError,'exists'):self.prepare()
        # Existing regular apply continues the selected VM rather than replacing it.
        final=setup.read(self.state/'timeweb-plan.json')
        tw.apply(self.state,final['plan_sha256'])
        self.assertEqual(self.created,[1,5])

    def test_run_out_and_preset_not_found_are_bounded_rejections(self):
        self.outcomes={1:TimewebError(400,'POST','servers_is_run_out_error'),5:TimewebError(404,'POST','preset_not_found')}
        review=self.prepare(max_attempts=2)
        result=self.run_plan(review)
        self.assertEqual(result['status'],'exhausted');self.assertEqual(self.created,[1,5])
        self.run_plan(review);self.assertEqual(self.created,[1,5])
        next_review=self.prepare()
        self.assertEqual([v['preset_id'] for v in next_review['candidates']],[6])

    def test_fatal_and_unknown_failures_do_not_try_another_candidate(self):
        for error in (TimewebError(403,'POST','forbidden'),TimewebError(400,'POST','quota_exceeded'),
                      TimewebError(402,'POST'),TimewebError(429,'POST'),TimewebError(500,'POST'),TimewebError(0,'POST')):
            with self.subTest(error=str(error)),tempfile.TemporaryDirectory() as folder:
                old=self.state;self.state=Path(folder)
                for name in ('deployment.json','timeweb-plan.json','id_ed25519','id_ed25519.pub'):
                    (self.state/name).write_bytes((old/name).read_bytes())
                self.api.items={};self.created=[];self.outcomes={1:error}
                try:
                    review=self.prepare();result=self.run_plan(review)
                    self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[1])
                    self.run_plan(review);self.assertEqual(self.created,[1])
                finally:self.state=old

    def test_price_change_and_late_matching_vm_block_before_post(self):
        review=self.prepare()
        self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]['price']=501
        result=self.run_plan(review)
        self.assertEqual(result['reason'],'catalog_or_price_changed');self.assertEqual(self.created,[])
        review=self.prepare()
        self.api.items['/api/v1/servers/99']={'server':{'id':99,'name':self.config['name']}}
        result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[])

    def test_deadline_survives_resume_and_is_checked_immediately_before_post(self):
        review=self.prepare(timeout=30)
        real=auto.save
        def stop(path,value):
            real(path,value)
            if path.name=='timeweb-auto-run.json':raise KeyboardInterrupt()
        with patch('timeweb_auto.save',side_effect=stop),self.assertRaises(KeyboardInterrupt):self.run_plan(review)
        self.now+=31
        result=self.run_plan(review)
        self.assertEqual(result['reason'],'time_limit');self.assertEqual(self.created,[])
        review=self.prepare(timeout=30)
        request=self.api.request
        def slow(path,method='GET',body=None):
            result=request(path,method=method,body=body)
            if path=='/api/v1/ssh-keys' and method=='POST':self.now+=31
            return result
        with patch.object(self.api,'request',side_effect=slow):result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[])

    def test_interrupted_transition_resumes_and_other_mutations_are_fenced(self):
        review=self.prepare();real=auto.save
        def stop(path,value):
            if path.name=='timeweb-journal.json':raise OSError('disk write interrupted')
            real(path,value)
        with patch('timeweb_auto.save',side_effect=stop),self.assertRaises(OSError):self.run_plan(review)
        for operation in (lambda:tw.plan(self.state),lambda:tw.apply(self.state),lambda:selection.select(self.state,5,'spb-3'),lambda:self.prepare()):
            with self.assertRaisesRegex(ValueError,'pending'):operation()
        result=self.run_plan(review)
        self.assertEqual(result['status'],'selected');self.assertEqual(self.created,[1])

    def test_crash_after_rejection_or_allocation_never_repeats_post(self):
        self.outcomes[1]=TimewebError(409,'POST','no_free_node')
        review=self.prepare();real=auto.save
        def stop(path,value):
            if path.name=='timeweb-auto-run.json' and value.get('index')==1:raise KeyboardInterrupt()
            real(path,value)
        with patch('timeweb_auto.save',side_effect=stop),self.assertRaises(KeyboardInterrupt):self.run_plan(review)
        def stop_success(path,value):
            if path.name=='timeweb-auto-run.json' and value.get('status')=='selected':raise KeyboardInterrupt()
            real(path,value)
        with patch('timeweb_auto.save',side_effect=stop_success),self.assertRaises(KeyboardInterrupt):self.run_plan(review)
        self.assertEqual(self.created,[1,5])
        result=self.run_plan(review)
        self.assertEqual(result['status'],'selected');self.assertEqual(self.created,[1,5])

    def test_lost_create_reply_with_real_vm_does_not_create_second(self):
        self.api.fail='/api/v1/servers'
        review=self.prepare();result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[1])
        self.assertTrue(setup.read(self.state/'timeweb-journal.json')['resources']['server']['submitted'])
        self.run_plan(review)
        with self.assertRaisesRegex(ValueError,'unknown'):self.prepare()
        self.assertEqual(self.created,[1])

    def test_incomplete_inventory_and_resource_failure_stop_before_vm(self):
        real=self.api.request
        def incomplete(path,method='GET',body=None):
            if path=='/api/v1/servers':return {'servers':[],'meta':{'total':1}}
            return real(path,method=method,body=body)
        review=self.prepare()
        with patch.object(self.api,'request',side_effect=incomplete):result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[])
        review=self.prepare()
        def denied(path,method='GET',body=None):
            if path=='/api/v1/projects' and method=='POST':raise TimewebError(409,'POST','no_free_node')
            return real(path,method=method,body=body)
        with patch.object(self.api,'request',side_effect=denied):result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[])

    def test_cleanup_and_changed_approved_plan_block(self):
        review=self.prepare()
        tampered=copy.deepcopy(review);tampered['max_attempts']=20
        setup.save(self.state/'timeweb-auto-plan.json',tampered)
        with self.assertRaisesRegex(ValueError,'exact'):self.run_plan(review)
        setup.save(self.state/'timeweb-cleanup.json',{'complete':True})
        with self.assertRaisesRegex(ValueError,'retired'):self.prepare()

    def test_old_rejected_state_is_reused_and_underpowered_or_overbudget_candidates_excluded(self):
        j=tw.Journal(self.state,self.config)
        j.value['resources']['server']={'submitted':False,'creation_rejection':{'status':400,'error_code':'servers_is_run_out_error'}}
        j.save()
        row=self.api.catalogs['/api/v1/presets/servers']['server_presets'][0]
        self.api.catalogs['/api/v1/presets/servers']['server_presets'] += [
            {**row,'id':7,'price':400,'ram':1024},{**row,'id':8,'price':9000}]
        review=self.prepare()
        self.assertEqual([v['preset_id'] for v in review['candidates']],[5,6])
        self.assertTrue(all(v['estimated_monthly_total'] <= 2000 for v in review['candidates']))
        result=self.run_plan(review)
        self.assertEqual(result['selected']['preset_id'],5)

    def test_kill_after_submit_intent_and_get_failure_after_allocation_never_fallback(self):
        real=self.api.request;review=self.prepare()
        def stop(path,method='GET',body=None):
            if path=='/api/v1/servers' and method=='POST':raise KeyboardInterrupt()
            return real(path,method=method,body=body)
        with patch.object(self.api,'request',side_effect=stop),self.assertRaises(KeyboardInterrupt):self.run_plan(review)
        result=self.run_plan(review)
        self.assertEqual(result['reason'],'creation_outcome_unknown');self.assertEqual(self.created,[])

    def test_get_failure_on_allocated_vm_keeps_server_and_stops(self):
        real=self.api.request;review=self.prepare()
        def fail(path,method='GET',body=None):
            if path.startswith('/api/v1/servers/') and method=='GET':raise TimewebError(409,'GET','no_free_node')
            return real(path,method=method,body=body)
        with patch.object(self.api,'request',side_effect=fail):result=self.run_plan(review)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[1])
        self.assertTrue(setup.read(self.state/'timeweb-journal.json')['resources']['server']['id'])
