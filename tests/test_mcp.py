import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp] for protocol integration tests")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, session, name, arguments=None):
        result = await session.call_tool(name, arguments or {})
        self.assertFalse(result.is_error, str(result))
        if result.structured_content is not None:
            return result.structured_content.get('result', result.structured_content)
        return json.loads(next(c.text for c in result.content if c.type == 'text'))

    async def test_two_clients_import_resume_enrich_then_update(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as temp, anyio.fail_after(60):
            root = Path(temp)
            exports = root / 'exports'
            exports.mkdir()
            fixture = exports / 'events.jsonl'
            records = [
                {'source': 'demo', 'instance': 'mcp', 'conversation_id': 'atlas', 'message_id': '1',
                 'role': 'user', 'title': 'Atlas', 'text': 'Atlas needs local storage.'},
                {'source': 'demo', 'instance': 'mcp', 'conversation_id': 'branch', 'message_id': '2',
                 'role': 'assistant', 'title': 'Atlas branch', 'text': 'SQLite is an option.',
                 'fork_of': {'conversation_id': 'atlas', 'message_id': '1'}},
                {'source': 'demo', 'instance': 'mcp', 'conversation_id': 'private', 'message_id': '3',
                 'role': 'user', 'title': 'Unrelated', 'text': 'Do not import this conversation.'}]
            fixture.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
            env = dict(os.environ)
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
            params = StdioServerParameters(command=sys.executable, args=[
                '-m', 'portable_context', '--home', str(root / 'memory'), 'mcp', '--source-root', str(exports)], env=env)
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as agent_a:
                    await agent_a.initialize()
                    status = await self.call(agent_a, 'memory_status')
                    workspace_id = status['workspace_id']
                    self.assertEqual(Path(status['storage']['server_directory']), (root / 'memory').resolve())
                    self.assertIsNone(status['storage']['country'])
                    setup = await agent_a.read_resource('portable-context://setup')
                    self.assertIn('storage', str(setup))
                    resource = await agent_a.read_resource('portable-context://guide')
                    self.assertIn('start_import', str(resource))
                    await self.call(agent_a, 'connect_source', {'source_id': 'archive', 'label': 'Selected archive',
                        'format': 'events', 'instance': 'mcp', 'location': str(fixture)})
                    preview = await self.call(agent_a, 'preview_source', {'source_id': 'archive'})
                    atlas = next(c['conversation_key'] for c in preview['conversations'] if c['conversation_id'] == 'atlas')
                    job = await self.call(agent_a, 'start_import', {'source_id': 'archive',
                        'conversation_keys': [atlas], 'include_forks': True, 'request_id': 'import-selected'})
                    partial = await self.call(agent_a, 'run_import', {'job_id': job['id'], 'batch_size': 1})
                    self.assertEqual(partial['status'], 'running')
            # Client A is gone. Client B connects to the SAME persisted workspace.
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as agent_b:
                    await agent_b.initialize()
                    status = await self.call(agent_b, 'memory_status')
                    self.assertEqual(status['workspace_id'], workspace_id)
                    done = await self.call(agent_b, 'run_import', {'job_id': job['id']})
                    self.assertEqual(done['import_counts']['inserted'], 2)
                    graph = await self.call(agent_b, 'inspect_graph', {'layer': 'structural'})
                    self.assertIn('forked_from', {e['predicate'] for e in graph['edges']})
                    enrichment = await self.call(agent_b, 'start_enrichment', {'request_id': 'enrich-first'})
                    # Scripted fixtures represent agent-authored synthesis; no external model is called.
                    for i in range(enrichment['total']):
                        claimed = await self.call(agent_b, 'claim_enrichment', {'job_id': enrichment['id'], 'worker': 'agent-b'})
                        task = claimed['task']
                        evidence = await self.call(agent_b, 'read_source_revision', task['sources'][0])
                        self.assertIn('json_fragment', evidence)
                        pages = [{'id': 'atlas-' + str(i), 'title': 'Atlas evidence ' + str(i),
                            'text': 'An Atlas source discusses storage. Verify its role before treating it as a decision.',
                            'kind': 'inference', 'sources': task['sources'], 'expected_version': 0,
                            'relations': [{'type': 'about', 'target': 'conversation:' + task['conversation_key']}]}]
                        await self.call(agent_b, 'commit_enrichment', {'job_id': enrichment['id'], 'position': task['position'],
                            'lease_token': task['lease_token'], 'pages': pages, 'summary': 'Linked source to Atlas wiki', 'reviews':[{**r,'status':'reviewed','detail':'Read fixture dialogue text.'} for r in task['sources']]})
                    status = await self.call(agent_b, 'memory_status')
                    self.assertEqual(status['conversations_needing_enrichment'], 0)
                    self.assertEqual(status['stats']['messages'], 2)
                    await self.call(agent_b, 'connect_source', {'source_id': 'host', 'label': 'Host bridge', 'format': 'events', 'instance': 'mcp'})
                    changed = dict(records[0], message_id='4', text='For Atlas, use SQLite; this is the final decision.')
                    staged = await self.call(agent_b, 'stage_records', {'source_id': 'host', 'records': [changed], 'request_id': 'new-message'})
                    await self.call(agent_b, 'run_import', {'job_id': staged['id']})
                    later = await self.call(agent_b, 'start_enrichment')
                    self.assertEqual(later['total'], 1)
                    task = (await self.call(agent_b, 'claim_enrichment', {'job_id': later['id'], 'worker': 'agent-b'}))['task']
                    old = await self.call(agent_b, 'read_wiki', {'page_id': 'atlas-0'})
                    await self.call(agent_b, 'commit_enrichment', {'job_id': later['id'], 'position': task['position'],
                        'lease_token': task['lease_token'], 'pages': [{'id': old['id'], 'title': old['title'],
                        'text': 'The user selected SQLite for Atlas.', 'kind': 'user_statement', 'sources': task['sources'],
                        'expected_version': old['version'], 'expected_hash': old['hash']}], 'summary': 'Updated decision'})
                    self.assertEqual((await self.call(agent_b, 'read_wiki', {'page_id': old['id']}))['version'], 2)
                    await self.call(agent_b, 'refresh_vault')
                    self.assertTrue((root / 'memory/vault/wiki/INDEX.md').exists())
                    self.assertTrue((root / 'memory/vault/GRAPH.md').exists())

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
                        "message_id": "1", "role": "user", "text": "I prefer local storage.",
                        "memory_project": {"id": "atlas", "name": "Atlas", "basis": "user_selection"},
                        "attachments": [{"name": "brief.txt", "role": "input", "purpose": "project brief"}]}]})
                    self.assertFalse(result.is_error, str(result))
                    projects = await self.call(session, 'list_projects')
                    project = await self.call(session, 'read_project', {'project_id': projects[0]['id']})
                    self.assertTrue(project['entry'])
                    files = await self.call(session, 'list_files', {'project_id': projects[0]['id']})
                    receipt = await self.call(session, 'store_attachment', {
                        'occurrence_id': files[0]['occurrences'][0]['id'], 'content_base64': 'QXRsYXMgYnJpZWY='})
                    self.assertTrue(receipt['local_stored'])
                    results = await self.call(session, 'search_files', {'query': 'Atlas', 'project_id': projects[0]['id']})
                    self.assertEqual(len(results), 1)
                    excerpts = await self.call(session, 'read_file_excerpts', {'sha256': receipt['sha256']})
                    self.assertEqual(excerpts[0]['text'], 'Atlas brief')
                    self.assertEqual((await self.call(session, 'read_file_bytes', {'sha256': receipt['sha256']}))['content_base64'], 'QXRsYXMgYnJpZWY=')
                    updated = await self.call(session, 'update_project', {'project_id': projects[0]['id'],
                        'title': 'Atlas', 'summary': 'A local storage project.', 'conversation_keys': [],
                        'expected_version': project['version'], 'actor': 'test-agent'})
                    self.assertEqual(updated['project']['version'], 2)
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
