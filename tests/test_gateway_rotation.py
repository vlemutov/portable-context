from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import gateway_rotation as rotation
from gateway import issue_credentials


class Backend:
    def __init__(self):
        self.server='initial';self.client='initial'
        self.server_trust={'initial'};self.client_trust={'initial'};self.calls=[]

    def connected(self):
        assert self.client in self.server_trust and self.server in self.client_trust

    def origin(self,certificate,trusts,revision,client):
        self.server=certificate.name;self.server_trust={p.name for p in trusts}
        self.connected();self.calls.append(revision);return True

    def edge(self,certificate,trusts,revision,journal):
        self.client=certificate.name;self.client_trust={p.name for p in trusts}
        self.connected();self.calls.append(revision);return True

    def verify(self,new,old,journal):
        self.connected();assert self.server==self.client==new.name
        assert self.server_trust==self.client_trust=={new.name}
        return True


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'rotation'
        issue_credentials(self.root/'original','memory.example')
        rotation.initialize(self.state,{'public_host':'memory.example'},self.root/'original')
        self.time=rotation.utc();self.backend=Backend()

    def controller(self):return rotation.Rotation(self.state,self.backend,lambda:self.time)

    def complete(self):
        for _ in range(12):
            result=self.controller().tick()
            self.backend.connected()
            if result['status']=='complete':return result
            self.time+=timedelta(minutes=16)
        self.fail('Rotation did not finish')

    def test_not_due_does_not_issue_or_touch_services(self):
        self.assertEqual(self.controller().tick()['status'],'not_due')
        self.assertFalse((self.state/'rotation.json').exists());self.assertFalse(self.backend.calls)

    def test_restarts_before_and_after_every_effect_preserve_connectivity(self):
        # Each simulated interruption occurs after the remote side may have changed,
        # while the local completion record has not yet been written.
        fired=set()
        def crash(stage,point):
            key=(stage,point)
            if key not in fired:
                fired.add(key);raise RuntimeError('simulated process death')
        generation=None
        for _ in range(50):
            try:result=self.controller().tick(force=True,interrupt=crash)
            except RuntimeError:result={}
            self.backend.connected()
            journal=rotation.read(self.state/'rotation.json')
            generation=generation or journal['new'];self.assertEqual(generation,journal['new'])
            self.time+=timedelta(minutes=16)
            if result.get('status')=='complete':break
        else:self.fail('Interrupted rotation did not recover')
        self.assertEqual(len(fired),2*len(rotation.STAGES))
        self.assertEqual(rotation.read(self.state/'active.json')['generation'],generation)
        self.assertFalse((self.state/'failure.json').exists())
        self.assertEqual(self.controller().tick()['status'],'not_due')

    def test_overlap_is_retained_during_drain(self):
        self.controller().tick(force=True)
        self.controller().tick();self.controller().tick()
        self.assertEqual(self.controller().tick()['status'],'waiting')
        self.assertEqual(len(self.backend.server_trust),2)
        self.assertEqual(len(self.backend.client_trust),2)
        self.time+=timedelta(minutes=16);self.complete()

    def test_tampered_generation_never_reaches_backend(self):
        (self.state/'generations/initial/edge-key.pem').write_text('not a key')
        with self.assertRaises(ValueError):self.controller().tick(force=True)
        self.assertFalse(self.backend.calls)
        self.assertNotIn('not a key',json.dumps(rotation.status(self.state)))

    def test_atomic_bundle_replay_and_collision(self):
        old=self.state/'generations/initial'
        p=rotation.materialize(self.state,'example',old,[old],'origin')
        self.assertEqual(p,rotation.materialize(self.state,'example',old,[old],'origin'))
        (p/'origin-key.pem').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            rotation.materialize(self.state,'example',old,[old],'origin')


class CloudRotationTests(unittest.TestCase):
    def test_provider_null_env_type_has_same_signature(self):
        from gateway_rotation_cloud import desired_signature
        a={'template':{'containers':[{'env':[{'name':'x','value':'y'}]}]}}
        b=json.loads(json.dumps(a));b['template']['containers'][0]['env'][0]['type']=None
        self.assertEqual(desired_signature(a),desired_signature(b))

    def test_lost_secret_and_publish_replies_reconcile_without_new_mutations(self):
        from cloud_gateway import CloudAPIError,container_request
        from gateway_rotation_cloud import CloudBackend
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);issue_credentials(root/'new','memory.example')
            config={'project_id':'00000000-0000-0000-0000-000000000001','container_name':'memory-edge',
                'container_id':'fixture','public_host':'memory.example','origin_ip':'127.0.0.1',
                'image':'owner.cr.cloud.ru/edge@sha256:'+'a'*64}
            current=container_request(config['project_id'],config['container_name'],config['image'],
                config=b'old',certificate=b'old',ca=b'old',key_secret='old-key')
            current.update(id='fixture',status='running');current['configuration']['ingress']['publicUri']='memory.example'
            class API:
                secret=None;creates=0;patches=0
                def request(self,service,path,method='GET',body=None):
                    if service=='secrets' and method=='GET':
                        if self.secret is None:raise CloudAPIError(404,'GET')
                        return self.secret
                    if service=='secrets' and method=='POST':
                        self.secret={'payload':body['payload']};self.creates+=1
                        raise OSError('lost creation reply')
                    if method=='PATCH':
                        current.update(body);current['configuration']['ingress']['publicUri']='memory.example'
                        self.patches+=1;raise OSError('lost publish reply')
                    return current
            backend=CloudBackend.__new__(CloudBackend);backend.state=root;backend.config=config;backend.api=API()
            backend.public_probe=lambda *args:True
            journal={'new':'generation-test'};keys=root/'new'
            with self.assertRaises(OSError):backend.edge(keys,[keys],'generation-test-edge_switch',journal)
            self.assertEqual(backend.api.creates,1)
            with self.assertRaises(OSError):backend.edge(keys,[keys],'generation-test-edge_switch',journal)
            self.assertTrue(backend.edge(keys,[keys],'generation-test-edge_switch',journal))
            self.assertEqual(backend.api.creates,1);self.assertEqual(backend.api.patches,1)
            self.assertTrue((root/'publish-generation-test-edge_switch.json').exists())

    def test_installer_ships_startup_resource_and_secrets_only_over_stdin(self):
        import rotation_setup
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);keys=root/'keys';issue_credentials(keys,'memory.example')
            rotation.write(root/'deployment.json',{})
            rotation.write(root/'installed.json',{'root':'/private/server','memory':{'workspace_id':'fixture'}})
            rotation.write(root/'options.json',{'public_host':'memory.example'})
            with patch.object(rotation_setup,'ssh_args',return_value=['ssh']), \
                 patch.object(rotation_setup,'command_env',return_value={'AUTH_KEY_ID':'test-key','AUTH_SECRET':'test-secret'}), \
                 patch.object(rotation_setup,'checked',return_value=subprocess.CompletedProcess([],0,stdout='{"installed":true}')) as call:
                result=rotation_setup.install(root,root/'options.json',keys)
            envelope=json.loads(call.call_args.kwargs['input'])
            self.assertIn('edge/entrypoint.sh',envelope['payload']['files'])
            self.assertNotIn('test-secret',str(call.call_args.args))
            self.assertNotIn('test-secret',json.dumps(result))


if __name__=='__main__':unittest.main()
