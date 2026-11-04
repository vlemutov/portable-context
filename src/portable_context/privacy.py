"""Local, deterministic best-effort secret screening. Never return matched values."""
import io
import json
import re
import zipfile

PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|$)')),
    ('provider_key', re.compile(r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b')),
    ('bearer', re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}')),
    ('credential', re.compile(r'''(?i)(?:["']?\b(?:password|passwd|secret_key|access_token|api_key|auth_secret)["']?\s*[:=]\s*["']?)([^\s"',;}]{8,})''')),
]


def screen_text(text, redact=False):
    counts = {}
    for kind, pattern in PATTERNS:
        count = len(pattern.findall(text))
        if count:
            counts[kind] = count
            if redact:
                text = pattern.sub('[REDACTED:' + kind + ']', text)
    return text, counts


def screen_value(value, redact=False):
    # Walk keys too: a credential in a metadata key must not survive a redaction.
    counts = {}
    def walk(v):
        if isinstance(v, str):
            result, found = screen_text(v, redact)
            for k, n in found.items():
                counts[k] = counts.get(k, 0) + n
            return result
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            out = {}
            for k, val in v.items():
                # Structured credentials may not be adjacent after recursion.
                if re.fullmatch(r'(?i)(password|passwd|secret_key|access_token|api_key|auth_secret)', k) and isinstance(val, str) and len(val) >= 8:
                    counts['credential'] = counts.get('credential', 0) + 1
                    out[walk(k)] = '[REDACTED:credential]' if redact else val
                else:
                    out[walk(k)] = walk(val)
            return out
        return v
    result = walk(value)
    return result, counts


def screen_bytes(data):
    """Inspect text and bounded ZIP members (including Office XML). Opaque data is explicit."""
    findings = {}
    inspected = 0
    def scan(raw):
        nonlocal inspected
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            return False
        inspected += 1
        _, counts = screen_text(text)
        try:
            _, structured = screen_value(json.loads(text))
            for k,n in structured.items():
                counts[k] = max(n, counts.get(k,0))
        except (ValueError, TypeError):
            pass
        for k,n in counts.items():
            findings[k] = findings.get(k,0) + n
        return True
    opaque = not scan(data)
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members)>10000 or sum(m.file_size for m in members)>128*1024*1024:
                return {'scan_limit':1}, 'partial'
            opaque = False
            for member in members:
                if not member.is_dir():
                    try:
                        opaque |= not scan(archive.read(member))
                    except (RuntimeError, zipfile.BadZipFile):
                        return {'unreadable_archive':1}, 'partial'
    return findings, 'partial' if opaque and inspected else ('opaque' if opaque else 'text_scanned')
