"""Offline checks for the sealed browser handoff and the reviewed installer boundary."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

BROWSER = Path(__file__).resolve().parents[1] / "deploy/browser-cloudru"
sys.path.insert(0, str(BROWSER))
import controller
import kit


def js(page, expression, payload):
    # Run the exact standalone HTML script with Web Crypto, without network or a DOM.
    script = page.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]
    source = ('const document = {getElementById:()=>({}), querySelector:()=>({})};\n'
              + script + '\nconst input=JSON.parse(require("node:fs").readFileSync(0,"utf8"));\n'
              + expression)
    result = subprocess.run(["node", "-e", source], input=json.dumps(payload),
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


class BrowserInstall(unittest.TestCase):
    def test_versioning_acl_and_bucket_policy_must_confirm_private_storage(self):
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            self.skipTest("backup extra required")
        cfg = dict(endpoint="http://127.0.0.1", region="fixture", access_key="fixture",
                   secret_key="fixture", bucket="fixture")
        s3 = Mock()
        s3.get_bucket_versioning.return_value = {"Status":"Enabled"}
        s3.get_bucket_acl.return_value = {"Grants":[]}
        with patch.object(boto3, "client", return_value=s3):
            for status in (True, None):
                s3.get_bucket_policy_status.return_value = {"PolicyStatus":{"IsPublic":status}}
                with self.assertRaises(controller.InputError):
                    controller.verify_bucket(cfg)
            s3.get_bucket_policy_status.return_value = {"PolicyStatus":{"IsPublic":False}}
            controller.verify_bucket(cfg)
            s3.get_bucket_policy.side_effect = ClientError({"Error":{"Code":"AccessDenied"}}, "GetBucketPolicy")
            with self.assertRaises(ClientError):
                controller.verify_bucket(cfg)
            s3.get_bucket_policy.side_effect = ClientError({"Error":{"Code":"NoSuchBucketPolicy"}}, "GetBucketPolicy")
            controller.verify_bucket(cfg)
            s3.get_bucket_acl.return_value = {"Grants":[{"Grantee":{"URI":"http://example.test/AllUsers"}}]}
            with self.assertRaises(controller.InputError):
                controller.verify_bucket(cfg)

    def test_browser_encrypts_cloud_init_and_guest_recovers_exact_input(self):
        if not shutil.which("node"):
            self.skipTest("requires Node with Web Crypto")
        try:
            import cryptography
        except ImportError:
            self.skipTest("deployment extra required")
        pw = "sample-offline-owner-password"
        data = {"bucket":"pc-demo-only", "secret":"synthetic-install-secret"}
        seed = js(BROWSER / "prepare.html", "encryptSettings(input.data,input.pw).then(x=>console.log(cloudConfig(x)))",
                  {"data":data, "pw":pw})
        self.assertTrue(seed.startswith("#cloud-config"))
        for value in (*data.values(), pw):
            self.assertNotIn(value, seed)
        sealed = next(s.split("content: ")[1] for s in seed.splitlines() if s.startswith("    content: "))
        self.assertEqual(controller.unseal(sealed, pw), data)
        with self.assertRaises(controller.InputError):
            controller.unseal(sealed, pw + "x")
        altered = bytearray(base64.b64decode(sealed))
        altered[-1] ^= 1
        with self.assertRaises(controller.InputError):
            controller.unseal(base64.b64encode(altered), pw)
        self.assertNotIn("terraform apply", seed)

    def test_inventory_binds_only_this_vm_and_one_security_group(self):
        identifier = "11111111-1111-4111-8111-111111111111"
        nic = {"project_id":"project", "vm":{"id":"machine"}, "ip_address":"10.0.0.2",
               "attach_external_ip":{"ip_address":"8.8.8.8"}, "security_groups":[{"id":identifier}]}
        def inspect(nics):
            return controller.project_target(nics, "project", {"machine"}, {"10.0.0.2"})
        self.assertEqual(inspect([nic])["security_group_id"], identifier)
        for nics in ([], [nic, nic], [dict(nic, project_id="other")], [dict(nic, vm={"id":"other"})],
                     [dict(nic, ip_address="10.0.0.3")], [dict(nic, security_groups=[])],
                     [dict(nic, attach_external_ip={"ip_address":"127.0.0.1"})]):
            with self.subTest(nics=nics), self.assertRaises(ValueError):
                inspect(nics)

    def test_apply_requires_reviewed_hash_and_only_new_ingress(self):
        plan = {"plan_sha256":"abc", "changes":[]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(controller.https_setup, "install") as api:
            state = Path(tmp)
            controller.setup.save(state/"https-plan.json", plan)
            with self.assertRaises(ValueError):
                controller.install(state, "wrong")
            for action in ("create", "update", "delete"):
                item = {"resource":"another.resource", "actions":[action]}
                controller.setup.save(state/"https-plan.json", dict(plan, changes=[item]))
                with self.assertRaises(ValueError):
                    controller.install(state, "abc")
            api.assert_not_called()
            controller.setup.save(state/"https-plan.json", plan)
            api.return_value = {"checks":{"public":{"unauthenticated_rejected":True}},
                                "recovery_kit":{"backup_verified":True}, "mcp_url":"https://test.invalid/mcp"}
            self.assertEqual(controller.install(state, "abc"), api.return_value["mcp_url"])
            api.assert_called_once_with(state, "abc")

    def test_browser_opens_encrypted_kit_from_guest(self):
        if not shutil.which("node"):
            self.skipTest("requires Node with Web Crypto")
        try:
            import cryptography
        except ImportError:
            self.skipTest("deployment extra required")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ("kit.json","s3-config.json","restic-password","README.txt")
            for name in names:
                (folder/name).write_bytes(("private fixture " + name).encode())
            envelope = kit.seal(folder, "test-password").decode("ascii")
            expr = 'decryptKit(input.file,input.pw).then(x=>console.log(Buffer.from(x).toString("base64")))'
            page = BROWSER / "open-kit.html"
            encoded = js(page, expr, {"file":envelope,"pw":"test-password"})
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded))) as archive:
                self.assertEqual(archive.namelist(), list(names))
                for name in names:
                    self.assertEqual(archive.read(name), (folder/name).read_bytes())
            with self.assertRaises(subprocess.CalledProcessError):
                js(page, expr, {"file":envelope,"pw":"wrong"})
