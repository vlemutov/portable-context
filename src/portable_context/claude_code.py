"""Version-tolerant local transcript reader. No execution of historical tool calls."""
import base64
from collections import Counter
import json
import os
from pathlib import Path
import re

from .core import canonical, digest, validate

FILE_TOOLS = {'Read':'input', 'Write':'output', 'Edit':'output', 'MultiEdit':'output', 'NotebookEdit':'output'}
SERVICE_TYPES = {'file-history-snapshot', 'queue-operation', 'progress', 'last-prompt', 'summary',
                 'custom-title', 'ai-title', 'agent-name', 'agent-color', 'pr-link', 'saved_hook_context'}


def default_root():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude'))) / 'projects'


def read_lines(path, warnings):
    with Path(path).open('rb') as stream:
        for number, line in enumerate(stream,1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b'\n') and not stream.read(1):
                    warnings.append({'type':'incomplete_tail', 'file':str(path), 'line':number})
                    break
                raise ValueError(f'Malformed transcript at line {number}; no silent skip') from None
            if not isinstance(item,dict):
                raise ValueError(f'Expected transcript object at line {number}')
            yield item


def discover(root=None):
    root=Path(root or default_root()).expanduser().resolve()
    files=sorted(root.rglob('*.jsonl')) if root.is_dir() else [root]
    groups={}
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root if root.is_dir() else root.parent):
            continue
        warnings=[]
        items=list(read_lines(path,warnings))
        first=next((x for x in items if x.get('sessionId')),None)
        if not first:
            continue
        sid=str(first['sessionId'])
        agent=path.stem if 'subagents' in path.parts or path.stem.startswith('agent-') else None
        cid=sid if not agent else sid+'/'+agent
        g=groups.setdefault(cid,{'conversation_id':cid,'session_id':sid,'title':cid,'paths':[],
                                 'cwd':first.get('cwd'),'subagent':bool(agent),'records':0,'warnings':[]})
        g['paths'].append(str(path))
        g['records']=max(g['records'],sum(x.get('type') in ('user','assistant','system') for x in items))
        g['warnings']+=warnings
        for x in items:
            if x.get('customTitle') or x.get('aiTitle'):
                g['title']=x.get('customTitle') or x.get('aiTitle')
    return list(groups.values())


def convert(paths, instance, title=None, thinking='exclude', subagent=False):
    """Merge copies by native UUID. Divergent copies are reported and require explicit selection."""
    if thinking not in ('exclude','archive'):
        raise ValueError('thinking must be exclude or archive')
    warnings=[]
    items={}
    service=Counter()
    session=None
    for path in paths:
        for item in read_lines(path,warnings):
            if session is None and item.get('sessionId'):
                session=str(item['sessionId'])
            if item.get('customTitle') or item.get('aiTitle'):
                title=item.get('customTitle') or item.get('aiTitle')
            native=item.get('uuid')
            typ=item.get('type','unknown')
            if not native:
                service[typ]+=1
                # Preserve non-message service records as explicitly typed events with stable content IDs.
                native='event-'+digest(canonical(item))
            if native in items and items[native]!=item:
                # Path-only relocation metadata is observation, not message content.
                old={k:v for k,v in items[native].items() if k not in ('cwd',)}
                new={k:v for k,v in item.items() if k not in ('cwd',)}
                if old!=new:
                    raise ValueError('Conflicting copies for native record '+native+'; select one transcript explicitly')
                continue
            items.setdefault(native,item)
    if session is None:
        raise ValueError('No sessionId in selected transcript')
    cid=session
    if subagent:
        cid=session+'/'+Path(paths[0]).stem
    records=[]
    for native,item in items.items():
        typ=item.get('type','unknown')
        if typ not in ('user','assistant','system') and typ not in SERVICE_TYPES:
            warnings.append({'type':'unknown_record_type','record_type':typ,'message_id':native})
        msg=item.get('message') or {}
        if not isinstance(msg,dict):
            msg={}
        content=msg.get('content',item.get('content',[]))
        blocks=[{'type':'text','text':content}] if isinstance(content,str) else content
        if not isinstance(blocks,list):
            blocks=[{'type':'unknown','value':blocks}]
        parts=[]
        payloads=[]
        attachments=[]
        kinds=set()
        excluded=0
        for pos,b in enumerate(blocks):
            if not isinstance(b,dict):
                b={'type':'unknown','value':b}
            kind=b.get('type','unknown')
            kinds.add(kind)
            if kind=='text':
                parts.append(str(b.get('text','')))
            elif kind=='thinking' or kind=='redacted_thinking':
                if thinking=='exclude':
                    excluded+=len(canonical(b))
                    continue
            if kind!='text':
                payloads.append({'block_index':pos,'kind':kind,'value':b,'searchable':kind not in ('thinking','redacted_thinking')})
                if kind=='tool_use':
                    name=b.get('name','unknown')
                    parts.append('[Tool call: '+name+']')
                    inp=b.get('input') or {}
                    if isinstance(inp,dict) and name in FILE_TOOLS:
                        fp=inp.get('file_path') or inp.get('notebook_path')
                        if isinstance(fp,str):
                            attachments.append({'path':fp,'role':FILE_TOOLS[name],'purpose':'Referenced by '+name,
                                                'status':'unknown','metadata_basis':'source_tool_call','reference_kind':'tool_file'})
                elif kind=='tool_result':
                    parts.append('[Tool result: '+str(b.get('tool_use_id','unknown'))+']')
                elif kind not in ('thinking','redacted_thinking'):
                    parts.append('['+kind+': archived payload]')
        role=typ if typ in ('user','assistant') else 'system'
        if typ=='user' and kinds and kinds <= {'tool_result'}:
            role='tool'
        elif typ=='user' and (item.get('isMeta') or re.fullmatch(r'\s*<(?:system-reminder|local-command-[^>]+|command-[^>]+)[\s\S]*', '\n'.join(parts))):
            role='system'
        raw={k:v for k,v in item.items() if k!='message'}
        raw['message_metadata']={k:v for k,v in msg.items() if k!='content'}
        # content also appears at top-level in some service events; archive it exactly once.
        raw.pop('content',None)
        if typ not in ('user','assistant'):
            payloads.append({'kind':'service_event','block_index':'service-event','value':item,'searchable':False})
        r={'source':'claude-code','instance':instance,'conversation_id':cid,'message_id':native,
           'parent_id':item.get('parentUuid'),'role':role,'text':'\n'.join(parts),'timestamp':item.get('timestamp'),
           'title':title or cid,'raw':raw,'attachments':attachments,'payloads':payloads,
           'capture_policy':{'adapter':'claude-code-v1','thinking':thinking},
           'completeness':{'excluded_thinking_chars':excluded,'source_record_type':typ}}
        if subagent:
            r['subagent_of']={'conversation_id':session}
        # Do not infer forks from names or shared prefixes.
        explicit=item.get('forkOf')
        if isinstance(explicit,dict) and isinstance(explicit.get('conversationId'),str):
            r['fork_of']={'conversation_id':explicit['conversationId']}
            if explicit.get('messageId'):
                r['fork_of']['message_id']=explicit['messageId']
        records.append(validate(r))
    return records,{'adapter':'claude-code-v1','warnings':warnings,'service_types':dict(service),
                    'records':len(records),'conversation_id':cid,'source_files':list(map(str,paths))}
