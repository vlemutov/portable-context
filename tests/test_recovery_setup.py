import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
from recovery_setup import receive_kit


class RecoverySetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.workspace='00000000-0000-4000-8000-000000000001'

    def payload(self,password=b'never-print-this-secret'):
        files={'restic-password':password,'s3-config.json':b'{"secret_key":"synthetic"}'}
        manifest={'format':'portable-context-identity-1','issuer':'https://memory.example',
            'repository':'s3:example/identity','memory':{'repository':'s3:example/memory','workspace_id':self.workspace},
            'files':{name:hashlib.sha256(value).hexdigest() for name,value in files.items()}}
        files['kit.json']=json.dumps(manifest).encode()
        return {'configured':True,'workspace_id':self.workspace,'verified':True,
            'kit_files':{name:base64.b64encode(value).decode() for name,value in files.items()}}

    def test_private_handoff_is_redacted_idempotent_and_preserves_old_keys(self):
        payload=self.payload();first=receive_kit(payload,self.root)
        self.assertNotIn('never-print',json.dumps(first))
        self.assertNotIn('synthetic',json.dumps(first))
        self.assertTrue((Path(first['private_directory'])/'README.txt').is_file())
        self.assertTrue(receive_kit(payload,self.root)['reused'])
        second=receive_kit(self.payload(b'rotated-secret'),self.root)
        self.assertNotEqual(first['private_directory'],second['private_directory'])
        self.assertEqual((Path(first['private_directory'])/'restic-password').read_bytes(),b'never-print-this-secret')
        self.assertEqual((Path(second['private_directory'])/'restic-password').read_bytes(),b'rotated-secret')

    def test_invalid_transport_never_creates_complete_kit(self):
        for kind in ('path','checksum','workspace','unverified'):
            with self.subTest(kind=kind):
                payload=self.payload()
                if kind=='path':payload['kit_files']['../escape']='eA=='
                if kind=='checksum':payload['kit_files']['restic-password']='eA=='
                if kind=='workspace':payload['workspace_id']='other'
                if kind=='unverified':payload['verified']=False
                with self.assertRaises(ValueError):receive_kit(payload,self.root)
                self.assertFalse((self.root/'recovery-kit').exists())

    def test_explicit_destination_is_never_overwritten(self):
        target=self.root/'chosen';receive_kit(self.payload(),self.root,target)
        with self.assertRaisesRegex(ValueError,'Existing kit differs'):
            receive_kit(self.payload(b'changed'),self.root,target)

    def test_no_oauth_is_explicit_not_success(self):
        value=receive_kit({'configured':False,'reason':'oauth_not_installed'},self.root)
        self.assertFalse(value['configured'])
        self.assertFalse((self.root/'recovery-kit').exists())
