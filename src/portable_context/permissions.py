"""Explicit MCP capability policy. Local stdio remains an owner-operated channel."""
import json


READ = frozenset('''memory_status backup_status list_backups get_backup_policy
get_index_policy list_projects list_project_groups preview_project_group_migration
get_capture_policy list_imports read_import_events read_import_scope list_source_sessions
capture_upload_status list_capture_receipts read_project list_files read_file search_files
read_file_excerpts read_file_bytes get_job list_jobs review_coverage read_source_revision
search_wiki read_wiki read_evidence inspect_graph check_memory search_messages read_message
list_conversations read_conversation search_memory read_memory forecast_import'''.split())
WRITE = frozenset('''create_project save_project_group archive_project begin_capture_upload
upload_capture_chunks finish_capture_upload update_project annotate_file add_file_excerpts
stage_records stage_export start_enrichment finish_enrichment claim_enrichment renew_work
commit_enrichment append_messages write_memory refresh_vault'''.split())
ADMIN = frozenset('''preview_backup_policy set_backup_policy backup_after_import
set_index_policy set_capture_policy cleanup_unused_placeholders import_file_capture
preview_source start_import verify_s3_original'''.split())
DYNAMIC = frozenset({'storage_report', 'store_attachment', 'connect_source', 'run_import'})
TOOLS = READ | WRITE | ADMIN | DYNAMIC
SCOPES = ('memory:read', 'memory:write', 'memory:admin')
READ_METHODS = frozenset({'initialize', 'ping', 'tools/list', 'resources/list',
    'resources/templates/list', 'resources/read', 'prompts/list', 'prompts/get',
    'completion/complete', 'logging/setLevel', 'notifications/initialized',
    'notifications/cancelled', 'notifications/progress'})
RESOURCES = frozenset({'portable-context://setup', 'portable-context://guide'})


def tool_scope(name, arguments, store):
    """Unknown operations fail closed; argument-sensitive cases precede dispatch."""
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    if name in READ:
        return SCOPES[0]
    if name in WRITE:
        return SCOPES[1]
    if name in ADMIN:
        return SCOPES[2]
    if name == 'storage_report':
        return SCOPES[2] if arguments.get('refresh_s3', False) else SCOPES[0]
    if name == 'store_attachment':
        return SCOPES[2] if arguments.get('server_path') is not None else SCOPES[1]
    if name == 'connect_source':
        if arguments.get('location') is not None:
            return SCOPES[2]
        # A writer may register a host source, but must not replace a local binding.
        with store.connect() as con:
            row = con.execute('SELECT mode FROM sources WHERE id=?', (arguments.get('source_id'),)).fetchone()
        return SCOPES[2] if row and row['mode'] != 'host' else SCOPES[1]
    if name == 'run_import':
        # Only already staged host input is runnable by an ordinary writer.
        with store.connect() as con:
            row = con.execute('SELECT specification FROM jobs WHERE id=?', (arguments.get('job_id'),)).fetchone()
        if row and json.loads(row['specification']).get('operation') == 'host_batch':
            return SCOPES[1]
        return SCOPES[2]
    return None


class CapabilityPolicy:
    def __init__(self, store, verifier):
        self.store, self.verifier = store, verifier

    async def __call__(self, ctx, call_next):
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.shared.exceptions import MCPError
        token = get_access_token()
        # Check again at dispatch: a queued tool must not rely on stale HTTP auth.
        token = await self.verifier.verify_token(token.token) if token else None
        if token is None:
            raise MCPError(-32001, 'Authentication required')
        params = ctx.params or {}
        required = SCOPES[0] if ctx.method in READ_METHODS else None
        if ctx.method == 'resources/read' and params.get('uri') not in RESOURCES:
            required = None
        if ctx.method == 'tools/call':
            arguments = params.get('arguments') or {}
            required = tool_scope(params.get('name'), arguments, self.store) if isinstance(arguments, dict) else None
        if required is None:
            raise MCPError(-32003, 'Operation is not authorized')
        if SCOPES[0] not in token.scopes or required not in token.scopes:
            raise MCPError(-32003, 'Insufficient scope', data={'required_scopes': [SCOPES[0], required]})
        return await call_next(ctx)
