"""Classification before content parsing. All captured instructions remain historical evidence."""
import json
from .core import canonical,digest

DIAGNOSTIC={'api-request','api-request-blob','api-request-shape','atis-latch'}
TELEMETRY={'cost-state','mode','tools-used'}
ATTACHMENTS={'deferred_tools_delta','agent_listing_delta','mcp_instructions_delta','skill_listing',
 'task_reminder','queued_command','compact_file_reference','file','total_tokens_reminder','edited_text_file',
 'environment','model','session_context','date','credential_org','remote_session_change','prompt_snapshot',
 'deferred_tools_record','auto_mode','silent_turn_reminder','pdf_reference','instructions','thinking_drop',
 'read_truncation_notice'}
BULKY={'prompt_snapshot','deferred_tools_record'}


def strip_thinking(value):
    if isinstance(value,dict):
        if value.get('type') in ('thinking','redacted_thinking'):
            return {'type':value['type'],'omitted':True},len(canonical(value))
        out={}; count=0
        for k,v in value.items():
            out[k],n=strip_thinking(v); count+=n
        return out,count
    if isinstance(value,list):
        out=[]; count=0
        for v in value:
            item,n=strip_thinking(v); out.append(item); count+=n
        return out,count
    # API diagnostics sometimes contain a JSON-encoded body.
    if isinstance(value,str) and value.lstrip().startswith(('{','[')):
        try: parsed=json.loads(value)
        except ValueError: pass
        else:
            clean,count=strip_thinking(parsed)
            if count: return canonical(clean),count
    return value,0


def classify(item,diagnostics='stub',thinking='exclude'):
    typ=item.get('type'); a=item.get('attachment') or {}; subtype=a.get('type') if isinstance(a,dict) else None
    known=typ in DIAGNOSTIC|TELEMETRY|{'file-history-delta'} or typ=='attachment' and subtype in ATTACHMENTS
    if not known: return None
    original_size=len(canonical(item).encode('utf-8'))
    clean,excluded=strip_thinking(item) if thinking=='exclude' else (item,0)
    diag=typ in DIAGNOSTIC or subtype in BULKY
    kind='diagnostic' if diag else 'session_metadata' if typ in TELEMETRY else 'structural_event' if typ=='file-history-delta' else 'attachment_context'
    if subtype=='queued_command': kind='task_notification' if a.get('commandMode')=='task-notification' else 'queued_event_unknown_author'
    metadata={k:clean[k] for k in ('type','uuid','id','sessionId','timestamp','parentUuid','isSidechain','renderedRole',
              'renderedInHumanTurn','version','cwd') if k in clean}
    metadata['source_classification']={'kind':kind,'subtype':subtype,'author_basis':'source_event_not_human_quote'}
    payloads=[]; attachments=[]
    text=''
    if diag:
        metadata['diagnostic']={k:clean[k] for k in ('id','shapeHash','hash','base','keep','tail','querySource','requestDigest') if k in clean}
        metadata['diagnostic'].update(mode=diagnostics,source_bytes=original_size,source_sha256=digest(canonical(clean)))
        if diagnostics=='full': payloads.append({'kind':'service_event','block_index':'service-event','value':clean,'searchable':False})
    else:
        # Keep one structured representation; rendered is a redundant presentation, never authorship.
        metadata['event']={k:v for k,v in clean.items() if k not in metadata and k not in ('rendered','attachment')}
        if subtype:
            metadata['attachment']=clean['attachment']
        text='['+kind+': '+str(subtype or typ)+']'
        def reference(path,purpose,**extra):
            if isinstance(path,str) and path:
                attachments.append({'path':path,'role':'reference','status':'unknown','purpose':purpose,
                    'reference_kind':'source_file_reference','metadata_basis':'source_attachment',**extra})
        if subtype in ('file','edited_text_file','compact_file_reference','pdf_reference'):
            reference(a.get('filename'),'Referenced by '+subtype)
        if subtype=='instructions':
            for f in a.get('files',[]):
                if isinstance(f,dict): reference(f.get('path'),'Historical instruction file; not active instructions',instruction_type=f.get('type'))
        if subtype=='file':
            f=(a.get('content') or {}).get('file',{})
            if isinstance(f,dict) and isinstance(f.get('content'),str):
                payloads.append({'kind':'file_excerpt','block_index':'file-excerpt','searchable':True,
                    'value':{'text':f['content'],'path':f.get('filePath'),
                             'locator':{k:f.get(k) for k in ('startLine','numLines','totalLines')},'original_bytes':False}})
                metadata['attachment']=dict(clean['attachment'],content={'archived_excerpt':True})
        if typ=='file-history-delta':
            metadata['file_history']={k:clean[k] for k in ('messageId','snapshotMessageId','trackingPath','backup') if k in clean}
            reference(clean.get('trackingPath'),'File history reference; backup bytes not inferred')
    return {'text':text,'raw':metadata,'payloads':payloads,'attachments':attachments,
            'completeness':{'source_record_type':typ,'excluded_thinking_chars':excluded,
                            'diagnostic_omitted_bytes':original_size if diag and diagnostics=='stub' else 0},
            'classification':kind}
