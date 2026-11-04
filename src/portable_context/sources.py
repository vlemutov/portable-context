"""Source discovery and selection; never confuse client history with server access."""
import hashlib
from pathlib import Path
import zipfile

from .core import key, validate
from .importers import FORMATS, MAX_MEMBER, MAX_ZIP_TEXT, candidate, parse

GUIDES = {
    'claude-code': 'Use the local client discover/prepare workflow for Claude Code JSONL. Explicitly select sessions before capture. Unknown records and incomplete tails are reported.',
    'claude': 'Connect a local official export JSON/ZIP, or have the host provide records it can actually read. Account-wide history is not granted by MCP.',
    'chatgpt': 'Connect an official conversation export JSON/ZIP. Saved memory, instructions and unavailable chats require separate capture.',
    'gemini': 'Connect Google Takeout My Activity / Gemini Apps JSON or HTML. Activity may omit replies and grouping; Antigravity is not needed.',
    'telegram': 'Connect Telegram Desktop JSON (single chat or chats.list), or its full export ZIP. Sender roles remain participants.',
    'codex': 'Connect local session JSONL directories separately for current and archived sessions. Paginated app-server storage is not yet a native adapter.',
    'ouroboros': 'Use a host bridge to send source-native persisted records with stable IDs. A version-specific history reader must be supplied by the host.',
    'hermes': 'Use MCP plus a host bridge with access to actual persisted history. A native history reader is not bundled.',
    'openclaw': 'Use MCP plus a host bridge with access to actual persisted history. Respect host exclusions and incognito settings.',
    'events': 'Normalized JSONL for any source. Preserve stable instance, conversation and message IDs.',
}


def source_files(location, fmt, store_root):
    root = Path(location)
    if not root.exists():
        raise ValueError('Source unavailable: file or directory does not exist')
    if root.resolve() != root:
        raise ValueError('Source location changed through a symlink; register its actual path again')
    if root == store_root or root.is_relative_to(store_root) or store_root.is_relative_to(root):
        raise ValueError('Source and memory directories must be separate')
    if root.is_file():
        return [root]
    files = [p for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()
             and p.resolve().is_relative_to(root) and candidate(str(p), fmt)]
    if not files:
        raise ValueError('No supported files; register a specific export file or ZIP')
    return files


def scan_source(source, store_root):
    """Read-only preview. Results and original files are not persisted here."""
    if source['mode'] != 'local':
        raise ValueError('This source needs host-provided data; the memory server cannot inspect the host')
    fmt, instance = source['format'], source['instance']
    warnings, records, inputs = [], [], []
    total_bytes = 0
    for path in source_files(source['location'], fmt, store_root):
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and candidate(m.filename, fmt)]
                if not members:
                    raise ValueError('No recognized data member in ZIP; register the extracted JSON explicitly')
                if sum(m.file_size for m in members) > MAX_ZIP_TEXT:
                    raise ValueError('Selected ZIP text exceeds 512 MiB')
                for member in members:
                    if member.file_size > MAX_MEMBER:
                        raise ValueError('Export member exceeds 128 MiB')
                    data = archive.read(member)
                    total_bytes += len(data)
                    if total_bytes > MAX_ZIP_TEXT:
                        raise ValueError('Source preview exceeds 512 MiB; register smaller export groups')
                    records.extend(validate(r) for r in parse(data.decode('utf-8-sig'), fmt, instance, member.filename, warnings))
                    inputs.append({'file': path.name, 'member': member.filename, 'sha256': hashlib.sha256(data).hexdigest()})
                warnings.append('Attachments remain references. Selected imports do not copy the entire source ZIP.')
        else:
            if path.stat().st_size > MAX_MEMBER:
                raise ValueError('Export file exceeds 128 MiB')
            data = path.read_bytes()
            total_bytes += len(data)
            if total_bytes > MAX_ZIP_TEXT:
                raise ValueError('Source preview exceeds 512 MiB; register smaller export groups')
            content = data.decode('utf-8-sig')
            records.extend(validate(r) for r in parse(content, fmt, instance, path.name, warnings))
            inputs.append({'file': path.name, 'sha256': hashlib.sha256(data).hexdigest()})
    return records, {'warnings': sorted(set(warnings)), 'inputs': inputs,
                     'coverage': 'Selected message records and their raw fields; no claim of full account coverage.'}


def conversation_key(record):
    return key(record['source'], record['instance'], record['conversation_id'])


def catalog(records, query='', limit=50, offset=0):
    groups = {}
    for r in records:
        cid = conversation_key(r)
        group = groups.setdefault(cid, {'conversation_key': cid, 'conversation_id': r['conversation_id'],
                                       'source': r['source'], 'instance': r['instance'], 'title': r['title'],
                                       'messages': 0, 'matches': not query, 'fork_of': r.get('fork_of')})
        group['messages'] += 1
        if query.casefold() in (r['title'] + '\n' + r['text']).casefold():
            group['matches'] = True
    matches = [v for v in groups.values() if v.pop('matches')]
    return {'conversations': matches[max(0, offset):max(0, offset) + max(1, min(limit, 200))],
            'total_conversations': len(matches), 'total_messages': len(records)}


def select_records(records, conversation_keys=None, include_forks=False):
    available = {conversation_key(r) for r in records}
    selected = available if conversation_keys is None else set(conversation_keys)
    if not selected.issubset(available):
        raise ValueError('Selected conversation is no longer available; preview the source again')
    if include_forks:
        while True:
            descendants = {conversation_key(r) for r in records if isinstance(r.get('fork_of'), dict)
                           and key(r['source'], r['instance'], r['fork_of']['conversation_id']) in selected}
            expanded = selected | descendants
            if expanded == selected:
                break
            selected = expanded
    return [r for r in records if conversation_key(r) in selected]
