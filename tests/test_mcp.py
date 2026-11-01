import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp] for protocol integration tests")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_write_search_read_memory(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as temp, anyio.fail_after(30):
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "portable_context", "--home", temp, "mcp"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertIn("append_messages", {t.name for t in tools.tools})
                    result = await session.call_tool("append_messages", {"records": [{
                        "source": "test", "instance": "mcp", "conversation_id": "chat",
                        "message_id": "1", "role": "user", "text": "I prefer local storage."}]})
                    self.assertFalse(result.is_error, str(result))
                    result = await session.call_tool("search_messages", {"query": "local storage"})
                    self.assertFalse(result.is_error, str(result))
                    payload = result.structured_content["result"]
                    mid = payload[0]["id"]
                    result = await session.call_tool("read_message", {"message_key": mid})
                    self.assertFalse(result.is_error, str(result))
                    result = await session.call_tool("write_memory", {
                        "note_id": "storage", "text": "User prefers local storage.",
                        "sources": [mid], "kind": "user_statement"})
                    self.assertFalse(result.is_error, str(result))
                    result = await session.call_tool("search_memory", {"query": "local storage"})
                    self.assertFalse(result.is_error, str(result))
                    self.assertIn("storage", str(result.content))
                    result = await session.call_tool("refresh_vault", {})
                    self.assertFalse(result.is_error, str(result))
                    self.assertTrue((Path(temp) / "vault" / "INDEX.md").exists())


if __name__ == "__main__":
    unittest.main()
