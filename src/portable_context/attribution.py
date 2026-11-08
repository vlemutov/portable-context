"""Derived source attribution; never written into immutable source records or hashes."""
import hashlib
import json
import re

MAX_RAW_BYTES = 8 * 1024 * 1024


def mixed_segments(text):
    """Only balanced leading application reminders; never guess from arbitrary XML in prose."""
    segments=[]; cursor=0
    pattern=re.compile(r'\s*<system-reminder>[\s\S]*?</system-reminder>')
    while match:=pattern.match(text,cursor):
        segments.append({'role':'system','start':cursor,'end':match.end()})
        cursor=match.end()
    if segments and text[cursor:].strip():
        if re.match(r'\s*<(?:task-notification|local-command[^>]*|command-[^>]*)>',text[cursor:]):
            return []
        segments.append({'role':'user','start':cursor,'end':len(text)})
        return segments
    return []


def display_text(record, info):
    segments=info.get('content_segments')
    return ''.join(record.get('text','')[s['start']:s['end']] for s in segments if s['role']=='user') if segments else record.get('text','')


def attribution(record, store=None):
    role = record.get('role', 'unknown')
    result = {'source_role': role, 'effective_role': role, 'content_kind': 'source_message',
              'attribution_basis': 'source_role', 'attribution_version': 1}
    if record.get('source') != 'claude-code':
        return result
    raw = record.get('raw')
    raw = raw if isinstance(raw, dict) else {}
    if raw.get('archived_payload'):
        # Resolve only the declared raw payload, from the local verified object store.
        # Never follow historical file paths or fetch remote content during recall.
        reference = raw['archived_payload']
        attachment = next((a for a in record.get('attachments', []) if isinstance(a, dict)
                           and a.get('file_id') == reference and a.get('kind') == 'full_raw'), {})
        sha = attachment.get('sha256', '')
        resolved = None
        if store is not None and isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha):
            path = store.root / 'originals' / sha
            try:
                if not path.is_symlink() and path.stat().st_size <= MAX_RAW_BYTES:
                    with path.open('rb') as stream:
                        data = stream.read(MAX_RAW_BYTES + 1)
                    if len(data) <= MAX_RAW_BYTES and hashlib.sha256(data).hexdigest() == sha:
                        value = json.loads(data)
                        if isinstance(value, dict):
                            resolved = value
            except (OSError, ValueError):
                pass
        if resolved is None:
            return {**result, 'effective_role': 'unknown', 'content_kind': 'unverified_source',
                    'attribution_basis': 'raw_payload_unavailable_or_unverified',
                    'attribution_warning': 'Do not infer human authorship from the envelope; inspect the exact raw payload.'}
        raw = resolved
    classification=raw.get('source_classification')
    if isinstance(classification,dict):
        return {**result,'effective_role':'system','content_kind':classification.get('kind','source_event'),
                'attribution_basis':'claude-code.source_event',
                'attribution_warning':'Historical event/context, not evidence of human authorship.'}
    # Old archives predate the 0.8 adapter taxonomy. Classify their native envelope in the view too.
    from .claude_records import DIAGNOSTIC,TELEMETRY
    if raw.get('type') in DIAGNOSTIC|TELEMETRY|{'attachment','file-history-delta'}:
        return {**result,'effective_role':'system','content_kind':'diagnostic' if raw.get('type') in DIAGNOSTIC else 'attachment_context',
                'attribution_basis':'legacy_source_event_type','attribution_warning':'Source application event, not a human quotation.'}
    if raw.get('isCompactSummary') is True or record.get('isCompactSummary') is True:
        return {**result, 'effective_role': 'assistant', 'content_kind': 'generated_summary',
                'attribution_basis': 'claude-code.isCompactSummary',
                'attribution_warning': 'Model-generated context summary, not a user statement or verbatim user quote.'}
    if raw.get('type')=='user' and not raw.get('isMeta'):
        segments=mixed_segments(record.get('text',''))
        if segments:
            return {**result,'effective_role':'user','content_kind':'mixed_user_context',
                    'source_envelope_role':'user',
                    'attribution_version':2,'attribution_basis':'source_user_envelope_and_balanced_reminder',
                    'content_segments':segments,
                    'attribution_warning':'Only user segments are human evidence; leading reminders are historical application context.'}
    return result


def view(record, store=None):
    """A recall view, not a serialization suitable for reimport as source evidence."""
    info = attribution(record, store)
    return {**record, **info, 'role': info['effective_role'], 'display_text':display_text(record,info)}


def citation_warnings(store, con, refs, kind):
    if kind != 'user_statement':
        return []
    warnings = []
    for ref in refs:
        row = con.execute('SELECT record FROM revisions WHERE id=? AND hash=?',
                          (ref['message_key'], ref['revision'])).fetchone()
        if row:
            info = attribution(json.loads(row[0]), store)
            if info['content_kind']=='mixed_user_context':
                span=ref.get('text_range',{})
                if not isinstance(span,dict): span={}
                if not any(s['role']=='user' and type(span.get('start')) is int and type(span.get('end')) is int
                           and s['start']<=span['start']<span['end']<=s['end'] for s in info['content_segments']):
                    warnings.append({**ref,'issue':'user_statement_attribution_needs_review',
                                     'content_kind':'mixed_user_context','basis':'Cite a text_range inside the user segment.'})
            if info['content_kind'] in ('generated_summary','unverified_source','diagnostic','session_metadata',
                                       'structural_event','attachment_context','task_notification','queued_event_unknown_author'):
                warnings.append({**ref, 'issue': 'user_statement_attribution_needs_review',
                                 'content_kind': info['content_kind'], 'basis': info['attribution_basis']})
    return warnings
