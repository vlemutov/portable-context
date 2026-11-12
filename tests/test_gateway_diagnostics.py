import json
import os
import ssl
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import gateway_diagnostics as diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_vm_overlap_verifies_both_exact_leaves_and_rejects_unrelated_or_retired(self):
        from gateway import issue_credentials
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);roots=state/'generations'
            names=['g-'+c*32 for c in 'abc']
            for name in names: issue_credentials(roots/name,'memory.example')
            (state/'active.json').write_text(json.dumps({'generation':names[0]}))
            journal={'old':names[0],'new':names[1],'stage':'drain','complete':False}
            (state/'rotation.json').write_text(json.dumps(journal))
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*_):pass
                def do_GET(self):
                    self.send_response(200);self.end_headers();self.wfile.write(b'origin-test')
            for name in names:
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(roots/name/'origin.pem',roots/name/'origin-key.pem')
                context.verify_mode=ssl.CERT_REQUIRED
                context.load_verify_locations(roots/names[1]/'edge-ca.pem')
                server.socket=context.wrap_socket(server.socket,server_side=True)
                thread=threading.Thread(target=server.serve_forever);thread.start()
                try:
                    result=diagnostics.probe('127.0.0.1',server.server_port,'memory.example','transport',context=diagnostics.vm_context(state))
                    self.assertEqual(result['ok'],name in names[:2],result)
                    wrong_host=diagnostics.probe('127.0.0.1',server.server_port,'other.example','transport',context=diagnostics.vm_context(state))
                    self.assertEqual(wrong_host.get('error'),'tls_verification')
                    if name==names[0]:
                        (state/'active.json').write_text(json.dumps({'generation':names[1]}))
                        (state/'rotation.json').write_text(json.dumps({**journal,'complete':True}))
                        retired=diagnostics.probe('127.0.0.1',server.server_port,'memory.example','transport',context=diagnostics.vm_context(state))
                        self.assertEqual(retired.get('error'),'tls_verification')
                        (state/'active.json').write_text(json.dumps({'generation':names[0]}))
                        (state/'rotation.json').write_text(json.dumps(journal))
                finally:server.shutdown();server.server_close();thread.join()

    def test_vm_probes_run_with_only_standard_library(self):
        from gateway import issue_credentials
        import gateway_rotation
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);issue_credentials(root/'seed','memory.example')
            gateway_rotation.initialize(root/'state',{'public_host':'memory.example','origin_port':1,'application_port':1},root/'seed')
            source=str(Path(__file__).resolve().parents[1]/'deploy')
            code="import sys,json;sys.path.insert(0,sys.argv[1]);from gateway_diagnostics import monitor;print(json.dumps(monitor('https://memory.example',sys.argv[3],1,.5,sys.argv[2])))"
            result=subprocess.run([sys.executable,'-I','-S','-c',code,source,str(root/'state'),str(root/'trace.jsonl')],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(set(json.loads(result.stdout)['probes']),{'vm-origin:transport','vm-mcp:metadata'})

    def test_probe_keeps_status_and_markers_but_discards_payload_headers_and_exception_text(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                calls.append((self.path, dict(self.headers)))
                self.send_response(503)
                self.send_header('Set-Cookie', 'session=VERY-SECRET')
                self.send_header('Authorization', 'Bearer VERY-SECRET')
                self.send_header('X-PC-Origin-Revision', 'origin-test')
                self.send_header('X-PC-Edge-Revision', 'edge-test')
                self.send_header('X-PC-Failure-Layer', 'origin')
                self.send_header('X-Request-ID', '0123456789abcdef0123456789abcdef')
                self.send_header('Location', 'https://example.com/?token=VERY-SECRET')
                self.end_headers();self.wfile.write(b'PRIVATE CONVERSATION VERY-SECRET')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever);thread.start()
        try:
            record = diagnostics.probe('127.0.0.1', server.server_port, 'memory.example', 'metadata')
        finally:
            server.shutdown();server.server_close();thread.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '/.well-known/oauth-protected-resource/mcp')
        self.assertNotIn('Authorization', calls[0][1])
        self.assertFalse(record['ok']);self.assertEqual(record['status'], 503)
        self.assertEqual(record['headers']['x-pc-failure-layer'], 'origin')
        self.assertNotIn('VERY-SECRET', json.dumps(record));self.assertNotIn('PRIVATE CONVERSATION', json.dumps(record))
        self.assertEqual(diagnostics.error_category(ValueError('VERY-SECRET')), 'probe_setup')

    def test_allowlist_rejects_unexpected_header_values(self):
        result = diagnostics.safe_headers([('X-PC-Edge-Revision','Bearer secret'), ('X-Request-ID','secret-token'),
            ('X-PC-Failure-Layer','memory-content'), ('Server','private hostname'), ('Cookie','secret')])
        self.assertEqual(result, {})

    def test_trace_is_bounded_and_never_replaces_existing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trace.jsonl'
            trace = diagnostics.Trace(path, max_bytes=180)
            for _ in range(10): trace.append({'target':'public','kind':'edge','ok':True})
            result = trace.close()
            self.assertLessEqual(path.stat().st_size, 180)
            self.assertGreater(result['dropped'], 0)
            self.assertEqual(result['probes']['public:edge']['requests'], 10)
            with self.assertRaises(FileExistsError): diagnostics.Trace(path)
            if os.name != 'nt': self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_arbitrary_paths_credentials_or_redirects(self):
        with self.assertRaises(KeyError): diagnostics.probe('127.0.0.1', 1, 'memory.example', '/private')
        with self.assertRaises(ValueError): diagnostics.monitor('https://user:secret@example.com','unused',1)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/secret');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try: result=diagnostics.probe('127.0.0.1',server.server_port,'memory.example','edge')
        finally: server.shutdown();server.server_close();thread.join()
        self.assertEqual(result['status'],302);self.assertFalse(result['ok']);self.assertNotIn('Location',result['headers'])


if __name__=='__main__': unittest.main()
