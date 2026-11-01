"""Scripted acceptance demo, not an LLM or a native provider integration.

Run from the installed checkout: python examples/agent_workflow_demo.py --home /private/demo
Uses a new directory so it cannot modify an existing personal memory.
"""
import argparse
import json
from pathlib import Path

from portable_context.core import Store, key
from portable_context.workspace import Workspace
from portable_context.wiki import read_page


def main():
    parser = argparse.ArgumentParser(description='Two scripted agents share and enrich a memory')
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    if args.home.exists():
        parser.error('Choose a new demo directory, not an existing memory')
    first = Workspace(Store(args.home))
    first.register_source('demo-host', 'Synthetic host history', 'events', 'demo')
    records = [
        {'source': 'demo', 'instance': 'demo', 'conversation_id': 'atlas', 'message_id': '1',
         'title': 'Project Atlas', 'role': 'user', 'text': 'Keep Atlas data locally.',
         'project': {'id': 'atlas', 'name': 'Atlas'}},
        {'source': 'demo', 'instance': 'demo', 'conversation_id': 'atlas-fork', 'message_id': '2',
         'title': 'Atlas database alternatives', 'role': 'assistant', 'text': 'SQLite could be suitable.',
         'fork_of': {'conversation_id': 'atlas', 'message_id': '1'}}]
    imported = first.stage_records('demo-host', records, 'demo-import')
    first.run_import(imported['id'], batch_size=1)
    second = Workspace(Store(args.home))
    imported = second.run_import(imported['id'])
    enrichment = second.start_enrichment(request_id='demo-enrich')
    while second.job(enrichment['id'])['status'] != 'completed':
        task = second.claim_enrichment(enrichment['id'], 'scripted-agent-b')['task']
        root = task['conversation_key'] == key('demo', 'demo', 'atlas')
        pid = 'atlas' if root else 'atlas-options'
        pages = [{'id': pid, 'title': 'Atlas' if root else 'Atlas alternatives',
                  'text': 'The user wants local storage for Atlas.' if root else 'An assistant suggested SQLite; this is not yet a user decision.',
                  'kind': 'user_statement' if root else 'assistant_statement',
                  'sources': task['sources'], 'expected_version': 0,
                  'relations': [{'type': 'about', 'target': 'conversation:' + task['conversation_key']}]}]
        second.commit_enrichment(enrichment['id'], task['position'], task['lease_token'], pages, 'Created sourced Atlas page')
    later = first.stage_records('demo-host', [dict(records[0], message_id='3', text='I choose SQLite for Atlas.')], 'demo-update')
    first.run_import(later['id'])
    updated = first.start_enrichment(request_id='demo-update-wiki')
    task = first.claim_enrichment(updated['id'], 'scripted-agent-a')['task']
    previous = read_page(first, 'atlas')
    first.commit_enrichment(updated['id'], task['position'], task['lease_token'], [{
        'id': 'atlas', 'title': 'Atlas', 'text': 'The user wants local storage and has now chosen SQLite for Atlas.',
        'kind': 'user_statement', 'sources': task['sources'],
        'expected_version': previous['version'], 'expected_hash': previous['hash'],
        'relations': [{'type': 'related_to', 'target': 'wiki:atlas-options'}]}], 'Recorded the new user decision')
    projection = first.refresh()
    print(json.dumps({'workspace_id': first.describe()['workspace_id'], 'import': imported['import_counts'],
                      'messages_after_update': first.store.stats()['messages'], 'wiki_page_version': read_page(first, 'atlas')['version'],
                      'pending_conversations': first.describe()['conversations_needing_enrichment'],
                      'vault': str(args.home.resolve() / 'vault'), 'projection': projection,
                      'note': 'All conversation content and synthesis in this demo are scripted synthetic fixtures.'}, indent=2))


if __name__ == '__main__':
    main()
