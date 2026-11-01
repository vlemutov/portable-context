"""CI-only end-to-end test against a disposable Linux SSH user; no cloud account needed."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('setup', Path(__file__).with_name('setup.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


async def append(state):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    entry = setup.read(state / 'client-config.json')['mcpServers']['portable-context']
    async with stdio_client(StdioServerParameters(**entry)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool('append_messages', {'records': [{
                'source': 'test', 'instance': 'ssh-smoke', 'conversation_id': 'ci', 'message_id': '1',
                'role': 'user', 'text': 'Keep this synthetic message across reinstall and restore.'}]})
            assert not result.is_error, result


def main():
    with tempfile.TemporaryDirectory() as tmp:
        state = setup.private_directory(Path(tmp) / 'deployment')
        config = setup.prepare(state, 'server')
        config.update(host='127.0.0.1', ssh_user=os.environ['PC_SMOKE_USER'],
                      identity_file=os.environ['PC_SMOKE_KEY'], ssh_port=int(os.environ.get('PC_SMOKE_PORT', '22')))
        setup.save(state / 'deployment.json', config)
        first = setup.install(state)
        asyncio.run(append(state))
        second = setup.install(state)
        assert first['workspace_id'] == second['workspace_id']
        result = asyncio.run(setup.check(state))
        assert result['messages'] == 1, result
        # Reinstall already made and restored a new backup. Also ensure cron has one entry.
        cron = subprocess.check_output(setup.ssh_args(state, config) + ['crontab -l'], text=True)
        assert cron.count('# portable-context-managed-backup') == 1
        print(json.dumps({'ssh_mcp': result, 'reinstall': 'preserved memory', 'backup_restore': 'passed'}))


if __name__ == '__main__':
    main()
