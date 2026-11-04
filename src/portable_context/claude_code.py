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
                 'custom-title', 'ai-title', 'agent-name', 'agent-color', 'pr-link', 'saved_hook_context','relocated'}

def executed_script(command,cwd):
    # Recognize a conservative literal invocation; never execute or expand historical shell text.
    pattern=r'''(?:^|[;&|]\s*)\s*(?:python(?:\d+(?:\.\d+)?)?|py|node|Rscript|bash|sh)\s+(?:-u\s+)?(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))'''
    result=[]
    for match in re.finditer(pattern,command):
        path=next(x for x in match.groups() if x is not None)
        if any(c in path for c in ('$','`','%')) or Path(path).suffix.lower() not in ('.py','.js','.r','.sh'): continue
        if not Path(path).is_absolute():
            if not cwd: continue
            path=str(Path(cwd)/path)
        result.append(path)
    return result


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


def inventory(paths,subagent=False):
    items={}
    sessions={}
    warnings=[]
    for path in paths:
        rows=list(read_lines(path,warnings))
        first=next((str(x['sessionId']) for x in rows if x.get('sessionId')),None)
        if first is None:
            warnings.append({'type':'missing_session_id','file':str(path)})
            continue
        current=first
        for item in rows:
            sid=str(item.get('sessionId') or current)
            suffix='/'+Path(path).stem if subagent else ''
            cid=sid+suffix
            g=sessions.setdefault(cid,{'native_id':cid,'session_id':sid,'titles':[],'relocations':[],
                'transitions':[],'cwd':item.get('cwd'),'paths':[]})
            if str(path) not in g['paths']:
                g['paths'].append(str(path))
            if sid!=current:
                transition={'from':current+suffix,'to':cid,'kind':'observed_session_transition',
                            'basis':'sessionId_change_in_same_transcript'}
                if transition not in g['transitions']:
                    g['transitions'].append(transition)
            current=sid
            native=item.get('uuid') or 'event-'+digest(canonical(item))
            identity=(cid,native)
            if identity in items:
                old={k:v for k,v in items[identity].items() if k!='cwd'}
                new={k:v for k,v in item.items() if k!='cwd'}
                if old!=new:
                    raise ValueError('Conflicting copies for native record '+native+'; select one transcript explicitly')
            else:
                items[identity]=item
            for field in ('customTitle','aiTitle'):
                if item.get(field):
                    t={'value':str(item[field]),'kind':field,'timestamp':item.get('timestamp'),'message_id':native}
                    if t not in g['titles']:
                        g['titles'].append(t)
            if item.get('type')=='relocated' or item.get('relocatedCwd'):
                observation={'cwd':item.get('cwd'),'relocated_cwd':item.get('relocatedCwd'),
                             'timestamp':item.get('timestamp'),'message_id':native}
                if observation not in g['relocations']:
                    g['relocations'].append(observation)
    for cid,g in sessions.items():
        custom=[t for t in g['titles'] if t['kind']=='customTitle' and t['value'].strip()]
        candidates=custom or [t for t in g['titles'] if t['value'].strip()]
        g['display_title']=candidates[-1]['value'] if candidates else cid
        g['title_rule']='last observed nonempty custom title, otherwise AI title, otherwise native ID'
    return items,sessions,warnings


def discover(root=None):
    root=Path(root or default_root()).expanduser().resolve()
    files=sorted(root.rglob('*.jsonl')) if root.is_dir() else [root]
    groups={}
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root if root.is_dir() else root.parent):
            continue
        agent=path.stem if 'subagents' in path.parts or path.stem.startswith('agent-') else None
        items,sessions,warnings=inventory([path],bool(agent))
        for cid,meta in sessions.items():
            g=groups.setdefault(cid,{'conversation_id':cid,'session_id':meta['session_id'],'title':cid,'paths':[],
                'cwd':meta['cwd'],'subagent':bool(agent),'records':0,'warnings':[],'titles':[],
                'relocations':[],'transitions':[]})
            g['paths'].append(str(path))
            g['records']=max(g['records'],sum(c==cid for c,_ in items))
            g['warnings']+=warnings
            for field in ('titles','relocations','transitions'):
                g[field]+=[x for x in meta[field] if x not in g[field]]
            choices=[t for t in g['titles'] if t['kind']=='customTitle'] or g['titles']
            g['title']=choices[-1]['value'] if choices else cid
    for g in groups.values():
        roots=set()
        for source in g['paths']:
            # Derive from the actual transcript directory, never reverse an encoded cwd name.
            parent=Path(source).parent
            if parent.name=='subagents': parent=parent.parent.parent
            candidate=parent/'memory'
            if candidate.is_dir() and not candidate.is_symlink(): roots.add(str(candidate.resolve()))
        g['memory_roots']=sorted(roots)
    return list(groups.values())


def convert(paths, instance, title=None, thinking='exclude', subagent=False, session_id=None):
    """Merge copies by native UUID. Divergent copies are reported and require explicit selection."""
    if thinking not in ('exclude','archive'):
        raise ValueError('thinking must be exclude or archive')
    items,sessions,warnings=inventory(paths,subagent)
    service=Counter()
    if not sessions:
        raise ValueError('No sessionId in selected transcript')
    if session_id is not None and session_id not in sessions:
        raise ValueError('Selected native session is not in this transcript')
    records=[]
    for (cid,native),item in items.items():
        if session_id is not None and cid!=session_id:
            continue
        session=sessions[cid]['session_id']
        typ=item.get('type','unknown')
        if not item.get('uuid'):
            service[typ]+=1
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
                    elif isinstance(inp,dict) and name in ('Bash','bash','shell') and isinstance(inp.get('command'),str):
                        for fp in executed_script(inp['command'],item.get('cwd')):
                            attachments.append({'path':fp,'role':'reference','purpose':'Script invoked by source tool call',
                                'status':'unknown','metadata_basis':'source_tool_call','reference_kind':'executed_script'})
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
           'title':cid,'raw':raw,'attachments':attachments,'payloads':payloads,
           'conversation_metadata':{k:v for k,v in sessions[cid].items() if k not in ('paths','cwd')},
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
    return records,{'adapter':'claude-code-v2','warnings':warnings,'service_types':dict(service),
                    'records':len(records),'conversation_ids':sorted({r['conversation_id'] for r in records}),
                    'source_files':list(map(str,paths))}
