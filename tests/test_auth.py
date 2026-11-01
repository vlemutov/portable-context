import importlib.util
import socket
import tempfile
import threading
import unittest

from portable_context.auth import BearerAuth


class AuthTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install .[mcp] for HTTP integration')
    async def test_authenticated_streamable_http_roundtrip(self):
        import anyio
        import httpx2
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from portable_context.core import Store
        from portable_context.mcp_server import build_server

        token = 'synthetic-http-mcp-token-123456789'
        with tempfile.TemporaryDirectory() as temp, anyio.fail_after(30):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.listen(128)
            app = BearerAuth(build_server(Store(temp)).streamable_http_app(), token)
            server = uvicorn.Server(uvicorn.Config(app, log_level='critical'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                while not server.started:
                    await anyio.sleep(0.05)
                url = f'http://127.0.0.1:{port}/mcp'
                async with httpx2.AsyncClient(trust_env=False) as unauthenticated:
                    response = await unauthenticated.post(url, json={})
                    self.assertEqual(response.status_code, 401)
                async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}, trust_env=False) as client:
                    async with streamable_http_client(url, http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool('memory_status', {})
                            self.assertFalse(result.is_error, str(result))
                            self.assertIn('workspace_id', str(result.content))
            finally:
                server.should_exit = True
                await anyio.to_thread.run_sync(lambda: thread.join(timeout=5))
                sock.close()
                self.assertFalse(thread.is_alive(), 'HTTP server did not stop')

    async def test_auth_blocks_unauthorized_http_and_preserves_lifespan(self):
        calls = []
        async def downstream(scope, receive, send):
            calls.append(scope['type'])
        app = BearerAuth(downstream, 'synthetic-mcp-token-123456789')
        output = []
        async def send(message):
            output.append(message)
        async def receive():
            return {'type': 'http.request', 'body': b''}
        await app({'type': 'http', 'headers': []}, receive, send)
        self.assertEqual(output[0]['status'], 401)
        self.assertEqual(calls, [])
        await app({'type': 'http', 'headers': [(b'authorization', b'Bearer synthetic-mcp-token-123456789')]}, receive, send)
        self.assertEqual(calls, ['http'])
        await app({'type': 'lifespan'}, receive, send)
        self.assertEqual(calls, ['http', 'lifespan'])

    def test_missing_token_rejected(self):
        with self.assertRaises(ValueError):
            BearerAuth(None, '')


if __name__ == '__main__':
    unittest.main()
