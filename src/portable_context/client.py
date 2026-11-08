"""Local selection, capture and resumable delivery. Run: python -m portable_context.client --help."""
import argparse
import base64
import json
from pathlib import Path
import sys

from .core import atomic_write, canonical, digest
from .capture import capture, load_package


async def call(session,name,args=None):
    result=await session.call_tool(name,args or {},read_timeout_seconds=600)
    if result.is_error:
        raise ValueError(name+' failed: '+str(result.content))
    if result.structured_content is not None:
        return result.structured_content.get('result',result.structured_content)
    return json.loads(next(c.text for c in result.content if c.type=='text'))


async def with_session(entry,operation):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(**entry)) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            return await operation(session)


async def connect(config,profile):
    entry=json.loads(Path(config).read_text(encoding='utf-8-sig'))['mcpServers']['portable-context']
    async def check(session):
        status=await call(session,'memory_status')
        p={'connection':entry,'workspace_id':status['workspace_id'],'server_version':status['version']}
        if Path(profile).exists():
            old=json.loads(Path(profile).read_text(encoding='utf-8'))
            if old['workspace_id']!=p['workspace_id']:
                raise ValueError('Profile already belongs to another memory; use a new profile path')
        atomic_write(Path(profile),canonical(p))
        return {'profile':str(profile),'workspace_id':p['workspace_id'],'server_version':p['server_version']}
    return await with_session(entry,check)


async def send(profile,directory,project_id=None,policy=None,include_paths=(),agent=None):
    profile=Path(profile)
    connection=json.loads(profile.read_text(encoding='utf-8'))
    directory=Path(directory).resolve()
    manifest,records=load_package(directory)
    delivery_key=digest(canonical([connection['workspace_id'],manifest,project_id]))
    pending_file=profile.parent/'deliveries'/(delivery_key+'.json')
    from .policy import Policy,agent_workspace,runtime_exclusion
    from .control import category
    from . import __version__
    policy=policy or Policy.load(None)
    if manifest.get('project_snapshot'):
        snap=manifest['project_snapshot']
        if snap['workspace_id']!=connection['workspace_id']: raise ValueError('Project package belongs to another memory')
        for a in snap['files']:
            record={'source':'project-files','instance':'local','conversation_id':'folder',
                'raw':{'cwd':str(Path(snap['root'])/Path(a['relative_path']).parent)}}
            allowed,_=policy.select(record,snap['project_id'])
            if not allowed['conversation'] or a.get('sha256') and not allowed['document_bytes']:
                raise ValueError('Current local policy forbids project capture; prepare again')
    included={str(Path(p).expanduser().resolve()) for p in include_paths}
    for r in records:
        permitted,_=policy.select(r,project_id)
        if not permitted['conversation']:
            raise ValueError('Current local policy excludes a packaged conversation; prepare a new snapshot')
        if not permitted['tool_payloads'] and (r.get('raw') or r.get('role')=='tool' and r.get('text')):
            raise ValueError('Current local policy forbids packaged tool/raw content; prepare again')
        for a in r.get('attachments',[]):
            if not isinstance(a,dict) or not a.get('export_observation',{}).get('blob'):
                continue
            if not permitted['tool_payloads' if category(a)=='source_payload' else 'document_bytes']:
                raise ValueError('Current local policy forbids packaged bytes; prepare again')
            if a.get('path') and runtime_exclusion(a['path'],a) and str(Path(a['path']).resolve()) not in included:
                raise ValueError('Package contains excluded agent workspace bytes; prepare again or explicitly include that path')
    async def delivery(session):
        status=await call(session,'memory_status')
        if status['workspace_id']!=connection['workspace_id']:
            raise ValueError('Connected to a different workspace; delivery refused')
        if 2 not in status.get('capture_protocol',{}).get('versions',[]):
            raise ValueError('Server needs Portable Context 0.5 or newer before capture-v2 delivery')
        options={'manifest':manifest}
        modern='capture_policy_v1' in status.get('capabilities',[])
        if manifest.get('project_snapshot') and 'project_folders_v1' not in status.get('capabilities',[]):
            raise ValueError('Project folder capture needs server 0.7')
        if not modern:
            raise ValueError('Client 0.6 requires server 0.6 capabilities for safe policy and native identity handling; upgrade the server first')
        if modern:
            options.update(project_id=project_id,executor={'client':'portable-context','version':__version__,'agent':agent})
        import uuid
        pending=json.loads(pending_file.read_text(encoding='utf-8')) if pending_file.exists() else {}
        if not pending or pending.get('complete'):
            pending={'run_id':str(uuid.uuid4()),'complete':False,'workspace_id':connection['workspace_id']}
        atomic_write(pending_file,canonical(pending))
        options['run_id']=pending['run_id']
        started=await call(session,'begin_capture_upload',options)
        uid=started['upload_id']
        offsets={}
        offset=0
        while True:
            page=await call(session,'capture_upload_status',{'upload_id':uid,'offset':offset,'limit':500})
            offsets.update((x['path'],x) for x in page['files'])
            offset=page['next_offset']
            if offset is None:
                break
        batch=[]
        batch_bytes=0
        for name,receipt in offsets.items():
            if receipt['verified']:
                continue
            if receipt['offset']==receipt['size'] and receipt['size']>0:
                raise ValueError('Remote file has wrong checksum; use a new package after investigating corruption')
            path=directory/name
            with path.open('rb') as stream:
                stream.seek(receipt['offset'])
                position=receipt['offset']
                while True:
                    data=stream.read(256*1024)
                    if not data and receipt['size']:
                        break
                    if batch and (batch_bytes+len(data)>768*1024 or len(batch)>=90):
                        await call(session,'upload_capture_chunks',{'upload_id':uid,'chunks':batch})
                        batch=[]
                        batch_bytes=0
                    batch.append({'path':name,'offset':position,'content_base64':base64.b64encode(data).decode()})
                    batch_bytes+=len(data)
                    position+=len(data)
                    if not data:
                        break
        if batch:
            await call(session,'upload_capture_chunks',{'upload_id':uid,'chunks':batch})
        previous=None
        while True:
            finish={'upload_id':uid,'project_id':project_id}
            if modern:
                finish['run_id']=started['run_id']
            result=await call(session,'finish_capture_upload',finish)
            atomic_write(profile.parent/'last-delivery.json',canonical(result))
            print(canonical({'phase':'import','remaining':result['remaining_transfers'],'complete':result['complete']}),file=sys.stderr,flush=True)
            if result['complete']:
                pending['complete']=True
                atomic_write(pending_file,canonical(pending))
                break
            marker=(result['remaining_transfers'],result.get('s3',{}).get('failed',0))
            if marker==previous:
                raise ValueError('Delivery made no progress; local originals remain saved. Inspect last-delivery.json and retry after repair')
            previous=marker
        return result
    try:
        return await with_session(connection['connection'],delivery)
    except Exception as exc:
        # Do not persist tool error text: it can contain source data or credentials.
        pending=json.loads(pending_file.read_text(encoding='utf-8')) if pending_file.exists() else {}
        atomic_write(profile.parent/'last-delivery.json',canonical({**pending,'complete':False,
            'status':'failed','error_type':type(exc).__name__,'retry':'Run send with the same package and profile'}))
        raise


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('project-preview','project-bind'):
        c=sub.add_parser(name)
        c.add_argument('--profile',type=Path,required=True); c.add_argument('--root',type=Path,required=True)
        c.add_argument('--project-id',required=True); c.add_argument('--include-large',action='store_true')
        c.add_argument('--exclude',action='append',default=[]); c.add_argument('--policy',type=Path)
        if name=='project-bind': c.add_argument('--approve-plan',required=True)
    c=sub.add_parser('project-prepare')
    c.add_argument('--profile',type=Path,required=True); c.add_argument('--binding',required=True)
    c.add_argument('--destination',type=Path,required=True); c.add_argument('--policy',type=Path)
    c.add_argument('--accept-growth',action='store_true'); c.add_argument('--agent')
    c=sub.add_parser('project-preferences')
    c.add_argument('--profile',type=Path,required=True); c.add_argument('--files',choices=('include','conversations-only'))
    c=sub.add_parser('project-bindings'); c.add_argument('--profile',type=Path,required=True)
    for name in ('discover','prepare'):
        c=sub.add_parser(name)
        c.add_argument('--format',choices=['claude-code','claude-web'],default='claude-code')
        c.add_argument('--root',type=Path)
        c.add_argument('--instance',required=True)
        if name=='prepare':
            group=c.add_mutually_exclusive_group(required=True)
            group.add_argument('--session',action='append')
            group.add_argument('--all',action='store_true')
            c.add_argument('--destination',type=Path,required=True)
            c.add_argument('--source-root',type=Path,action='append',default=[])
            c.add_argument('--secrets',choices=['block','redact'],default='block')
            c.add_argument('--thinking',choices=['exclude','archive'],default='exclude')
            c.add_argument('--diagnostics',choices=['stub','full'],default='stub')
            c.add_argument('--project-id')
            c.add_argument('--project-name')
            c.add_argument('--policy',type=Path)
            c.add_argument('--include-path',type=Path,action='append',default=[])
            c.add_argument('--agent',help='Self-reported invoking agent, not authenticated identity')
    c=sub.add_parser('connect')
    c.add_argument('--config',type=Path,required=True)
    c.add_argument('--profile',type=Path,required=True)
    c=sub.add_parser('send')
    c.add_argument('--profile',type=Path,required=True)
    c.add_argument('--package',type=Path,required=True)
    c.add_argument('--project-id',help='Existing server project ID; not a source-native ID')
    c.add_argument('--policy',type=Path)
    c.add_argument('--include-path',type=Path,action='append',default=[])
    c.add_argument('--agent')
    c=sub.add_parser('upgrade-capture',help='Repair 0.5.0 payload IDs without re-reading source sessions')
    c.add_argument('--package',type=Path,required=True)
    c.add_argument('--destination',type=Path,required=True)
    args=p.parse_args(argv)
    if args.command.startswith('project-'):
        from . import project_files
        from .policy import Policy
        connection=json.loads(args.profile.read_text(encoding='utf-8'))
        if args.command=='project-bindings':
            result={'bindings':[json.loads(p.read_text(encoding='utf-8')) for p in sorted((args.profile.parent/'project-bindings').glob('*.json'))
                if json.loads(p.read_text(encoding='utf-8')).get('workspace_id')==connection['workspace_id']]}
        elif args.command=='project-preferences':
            path=args.profile.parent/'project-preferences.json'
            result=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'files':'include','accepted':False}
            if args.files:
                result={'files':args.files,'accepted':True}; atomic_write(path,canonical(result))
        elif args.command=='project-preview':
            result=project_files.preview(args.root,args.project_id,connection['workspace_id'],Policy.load(args.policy),args.include_large,args.exclude)
        elif args.command=='project-bind':
            result=project_files.bind(args.profile,args.root,args.project_id,args.approve_plan,args.include_large,args.exclude,Policy.load(args.policy))
        else:
            result=project_files.prepare(args.profile,args.binding,args.destination,Policy.load(args.policy),args.accept_growth,args.agent)
    elif args.command=='upgrade-capture':
        from .capture import upgrade_capture
        result=upgrade_capture(args.package,args.destination)
    elif args.command in ('connect','send'):
        import asyncio
        from .policy import Policy
        result=asyncio.run(connect(args.config,args.profile) if args.command=='connect' else
            send(args.profile,args.package,args.project_id,Policy.load(args.policy),args.include_path,args.agent))
    else:
        from .claude_code import discover,convert
        if args.format=='claude-code':
            groups=discover(args.root)
            if args.command=='discover':
                result={'conversations':groups}
            else:
                wanted={g['conversation_id'] for g in groups} if args.all else set(args.session)
                if not wanted.issubset({g['conversation_id'] for g in groups}):
                    raise ValueError('Unknown selected session; run discover again')
                records=[]
                reports=[]
                for g in groups:
                    if g['conversation_id'] in wanted:
                        converted,report=convert(g['paths'],args.instance,g['title'],args.thinking,g['subagent'],g['conversation_id'],args.diagnostics)
                        records+=converted
                        reports.append(report)
        else:
            if args.root is None:
                p.error('claude-web requires --root pointing to the local export')
            from .sources import scan_source,catalog
            records,report=scan_source({'mode':'local','format':'claude','instance':args.instance,'location':str(args.root.resolve())},
                                      args.root.resolve().parent/'.pc-preview-placeholder')
            if args.command=='discover':
                # Return all selected archive titles locally; nothing is sent to the server.
                first=catalog(records,limit=200)
                groups=first['conversations']
                for offset in range(200,first['total_conversations'],200):
                    groups+=catalog(records,limit=200,offset=offset)['conversations']
                result={'conversations':groups,'total_conversations':first['total_conversations'],'report':report}
            else:
                wanted={r['conversation_id'] for r in records} if args.all else set(args.session)
                if not wanted.issubset({r['conversation_id'] for r in records}):
                    raise ValueError('Unknown archive conversation ID')
                records=[r for r in records if r['conversation_id'] in wanted]
                reports=[report]
        if args.command=='prepare':
            project=None
            if args.project_id or args.project_name:
                if not args.project_id:
                    p.error('--project-id is required')
                project={'id':args.project_id,'basis':'user_selection'}
                if args.project_name: project['name']=args.project_name
                else: project['existing']=True
            from .policy import Policy
            from . import __version__
            result=capture(records,args.destination,args.source_root,project,secret_policy=args.secrets,adapter_report=reports,
                policy=Policy.load(args.policy),include_paths=args.include_path,
                executor={'client':'portable-context','version':__version__,'agent':args.agent})
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
