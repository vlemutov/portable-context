"""Derived source attribution; never written into immutable source records or hashes."""
import hashlib
import json
import re

MAX_RAW_BYTES = 8 * 1024 * 1024


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
    if raw.get('isCompactSummary') is True or record.get('isCompactSummary') is True:
        return {**result, 'effective_role': 'assistant', 'content_kind': 'generated_summary',
                'attribution_basis': 'claude-code.isCompactSummary',
                'attribution_warning': 'Model-generated context summary, not a user statement or verbatim user quote.'}
    return result


def view(record, store=None):
    """A recall view, not a serialization suitable for reimport as source evidence."""
    info = attribution(record, store)
    return {**record, **info, 'role': info['effective_role']}


def citation_warnings(store, con, refs, kind):
    if kind != 'user_statement':
        return []
    warnings = []
    for ref in refs:
        row = con.execute('SELECT record FROM revisions WHERE id=? AND hash=?',
                          (ref['message_key'], ref['revision'])).fetchone()
        if row:
            info = attribution(json.loads(row[0]), store)
            if info['content_kind'] in ('generated_summary', 'unverified_source'):
                warnings.append({**ref, 'issue': 'user_statement_attribution_needs_review',
                                 'content_kind': info['content_kind'], 'basis': info['attribution_basis']})
    return warnings
