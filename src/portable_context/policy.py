"""Explicit capture controls. Policies are configuration, never instructions from a transcript."""
import json
import os
from pathlib import Path, PureWindowsPath
import re
import ntpath
import posixpath

from .core import canonical, digest, key, now
from .control import category

AXES=('conversation','tool_payloads','document_bytes')
FULL=dict.fromkeys(AXES,True)
PRESETS={'full':FULL,'exclude':dict.fromkeys(AXES,False),
         'conversation_and_references':dict(conversation=True,tool_payloads=False,document_bytes=False),
         'no_document_bytes':dict(conversation=True,tool_payloads=True,document_bytes=False),
         'no_tool_payloads':dict(conversation=True,tool_payloads=False,document_bytes=True)}


def axes(value):
    if isinstance(value,str):
        if value not in PRESETS:
            raise ValueError('Unknown capture preset')
        return dict(PRESETS[value])
    if not isinstance(value,dict) or set(value)-set(AXES) or any(type(v) is not bool for v in value.values()):
        raise ValueError('Capture permissions must be boolean conversation/tool_payloads/document_bytes')
    result={**FULL,**value}
    if not result['conversation']:
        return dict.fromkeys(AXES,False)
    return result


def normalized_path(value):
    s=str(value).replace('\\','/')
    windows=bool(re.match(r'^[a-zA-Z]:/',s) or s.startswith('//'))
    s=(ntpath.normpath(s).replace('\\','/') if windows else posixpath.normpath(s)).rstrip('/')
    return s.casefold() if windows else s


def within(path,root):
    path,root=normalized_path(path),normalized_path(root)
    return path==root or path.startswith(root+'/')


class Policy:
    def __init__(self,config=None):
        self.config=config if config is not None else {'version':1,'default':'full','rules':[]}
        if self.config.get('version')!=1 or set(self.config)-{'version','default','rules'}:
            raise ValueError('Unsupported capture policy; expected version 1')
        self.default=axes(self.config.get('default','full'))
        self.rules=self.config.get('rules',[])
        if not isinstance(self.rules,list):
            raise ValueError('Policy rules must be a list')
        for rule in self.rules:
            if not isinstance(rule,dict) or set(rule)-{'id','source','instance','root','project','permissions'}:
                raise ValueError('Unknown policy rule field')
            if not any(rule.get(s) for s in ('source','instance','root','project')):
                raise ValueError('Each rule needs a selector')
            for field in ('id','source','instance','root','project'):
                if field in rule and (not isinstance(rule[field],str) or not rule[field].strip()):
                    raise ValueError('Policy selectors must be nonempty strings')
            if 'root' in rule:
                p=rule['root']
                if not (Path(p).is_absolute() or PureWindowsPath(p).is_absolute()):
                    raise ValueError('Policy root must be absolute')
            axes(rule['permissions'])
        self.fingerprint=digest(canonical(self.config))

    @classmethod
    def load(cls,path):
        path=path or os.environ.get('PC_CAPTURE_POLICY')
        default=Path.home()/'.portable-context/capture-policy.json'
        path=path or (default if default.exists() else None)
        return cls(json.loads(Path(path).read_text(encoding='utf-8-sig'))) if path else cls()

    def select(self,record,project_id=None):
        context=record.get('capture_context') or {}
        raw=record.get('raw') if isinstance(record.get('raw'),dict) else {}
        cwd=context.get('cwd') or raw.get('cwd')
        if cwd and Path(cwd).is_absolute():
            cwd=str(Path(cwd).resolve())
        project=project_id or context.get('project') or (record.get('memory_project') or {}).get('id')
        matched=[]
        for i,rule in enumerate(self.rules):
            if any(rule.get(k) is not None and rule[k]!=record.get(k) for k in ('source','instance')):
                continue
            if rule.get('project') is not None and rule['project']!=project:
                continue
            if 'root' in rule:
                if not cwd:
                    raise ValueError('Cannot evaluate folder policy without source working directory; recapture with a supported adapter')
                root=str(Path(rule['root']).resolve()) if Path(rule['root']).is_absolute() else rule['root']
                if not cwd or not within(cwd,root):
                    continue
            matched.append((rule.get('id',str(i)),axes(rule['permissions'])))
        return ({k:all(p[k] for _,p in matched) for k in AXES} if matched else dict(self.default),
                [name for name,_ in matched])


def agent_workspace(path):
    """Narrow known runtime locations, not all OS temporary directories."""
    s=normalized_path(path)
    folded=s.casefold()
    if '/.claude/projects/' in folded and ('/tool-results/' in folded or folded.endswith('.jsonl')):
        return 'claude_session_storage'
    if '/.codex/' in folded and any('/.codex/'+p+'/' in folded for p in ('sessions','archived_sessions','tmp')):
        return 'codex_runtime'
    if re.search(r'/(?:temp|tmp)/(?:claude|claude-[^/]+)(?:/|$)',folded):
        return 'claude_temporary_workspace'
    # macOS per-user temp lives below /var/folders/.../T.
    if re.search(r'/var/folders/.*/t/claude(?:-[^/]+)?(?:/|$)',folded):
        return 'claude_temporary_workspace'
    for variable,suffixes in (('CLAUDE_CONFIG_DIR',('projects',)),('CODEX_HOME',('sessions','archived_sessions','tmp'))):
        root=os.environ.get(variable)
        if root and any(within(path,str(Path(root)/part)) for part in suffixes):
            if variable=='CODEX_HOME' or '/tool-results/' in folded or folded.endswith('.jsonl'):
                return variable.lower()+'_runtime'
    return None


def permissions(con,project_id=None):
    values=[]
    for target in ('workspace', 'project:'+project_id if project_id else 'workspace'):
        row=con.execute('SELECT policy FROM capture_policies WHERE target=?',(target,)).fetchone()
        values.append(axes(json.loads(row[0])) if row else FULL)
    return {k:all(p[k] for p in values) for k in AXES}


def project_for(con,r,override=None):
    if override:
        if not con.execute('SELECT 1 FROM catalog_projects WHERE id=?',(override,)).fetchone():
            raise ValueError('Unknown target project')
        return override
    cid=r['conversation_id']
    alias=con.execute('SELECT conversation_id FROM source_sessions WHERE source=? AND instance=? AND native_id=?',
                      (r['source'],r['instance'],cid)).fetchone()
    cid=alias[0] if alias else cid
    assigned=con.execute('SELECT project_id FROM catalog_assignments WHERE conversation=?',
                         (key(r['source'],r['instance'],cid),)).fetchone()
    if assigned:
        return assigned[0]
    p=r.get('project')
    if p:
        return key('native-project',r['source'],r['instance'],p['id'])
    p=r.get('memory_project')
    return key('memory-project',p['id']) if p else None


def check_record(con,r,project_id=None):
    p=permissions(con,project_for(con,r,project_id))
    if not p['conversation']:
        raise ValueError('Capture policy excludes this project or workspace')
    if not p['tool_payloads'] and (r.get('payloads') or r.get('raw') or r.get('role')=='tool' and r.get('text')):
        raise ValueError('Capture policy forbids tool/raw payloads; prepare a restricted snapshot locally')
    for a in r.get('attachments',[]):
        if not isinstance(a,dict):
            continue
        has_bytes=bool(a.get('content_base64') or a.get('export_observation',{}).get('blob') or
                       a.get('sha256') and a.get('availability') not in ('excluded','unavailable','reference_only'))
        kind=category(a)
        if has_bytes and not p['tool_payloads' if kind=='source_payload' else 'document_bytes']:
            raise ValueError('Capture policy forbids these attachment bytes')


def check_attachment(con,occurrence):
    row=con.execute('SELECT record FROM messages WHERE id=?',(occurrence['message_key'],)).fetchone()
    p=permissions(con,project_for(con,json.loads(row[0])))
    if occurrence['availability']=='excluded' or not p['conversation'] or not p[
            'tool_payloads' if occurrence['category']=='source_payload' else 'document_bytes']:
        raise ValueError('Capture policy forbids attachment transfer')


def check_manifest(con,manifest,project_id=None):
    scopes=manifest.get('capture_scopes')
    if scopes is None:
        restricted=any(axes(json.loads(r[0]))!=FULL for r in con.execute('SELECT policy FROM capture_policies'))
        if restricted:
            raise ValueError('Legacy package cannot prove policy compliance before upload; prepare with client 0.6')
        return
    if not isinstance(scopes,list) or not scopes and manifest.get('messages'):
        raise ValueError('Capture scope declarations missing')
    for scope in scopes:
        p=permissions(con,project_for(con,scope,project_id))
        declared=axes(scope['permissions'])
        if not p['conversation'] or any(declared[k] and not p[k] for k in AXES):
            raise ValueError('Server capture policy rejects this package before byte upload')


def set_policy(store,policy,actor,expected_version=0,project_id=None):
    value=axes(policy)
    if not actor.strip():
        raise ValueError('Policy change needs an actor')
    target='project:'+project_id if project_id else 'workspace'
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if project_id and not con.execute('SELECT 1 FROM catalog_projects WHERE id=?',(project_id,)).fetchone():
            raise ValueError('Unknown project')
        old=con.execute('SELECT * FROM capture_policies WHERE target=?',(target,)).fetchone()
        if (old['version'] if old else 0)!=expected_version:
            raise ValueError('Policy version conflict')
        con.execute('INSERT OR REPLACE INTO capture_policies VALUES (?,?,?,?)',
                    (target,canonical(value),expected_version+1,now()))
        from .catalog import _audit
        _audit(con,actor,'capture_policy_changed',{'target':target,'previous':dict(old) if old else None,'permissions':value})
    return get_policy(store,project_id)


def get_policy(store,project_id=None):
    with store.connect() as con:
        target='project:'+project_id if project_id else 'workspace'
        row=con.execute('SELECT * FROM capture_policies WHERE target=?',(target,)).fetchone()
        return {'target':target,'version':row['version'] if row else 0,
                'configured':json.loads(row['policy']) if row else dict(FULL),'effective':permissions(con,project_id),
                'scope':'Future writes. Existing originals and backups are not deleted. Text may itself contain file contents.'}
