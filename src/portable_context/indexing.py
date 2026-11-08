"""Rebuildable search policy. Exact source bytes and citations are never removed."""
import json
from .core import canonical,now

KINDS=('service_event','tool_use','tool_result','full_raw','full_text','thinking','redacted_thinking','file_excerpt')

def initialize(con):
    fresh=not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_policy'").fetchone()
    con.execute('CREATE TABLE IF NOT EXISTS index_policy(kind TEXT PRIMARY KEY,enabled INTEGER NOT NULL,version INTEGER NOT NULL)')
    for kind in KINDS:
        con.execute('INSERT OR IGNORE INTO index_policy VALUES (?,?,0)',(kind,int(kind not in ('service_event','thinking','redacted_thinking'))))
    if fresh:
        for row in con.execute('SELECT DISTINCT sha256 FROM catalog_chunks_fts').fetchall():
            if not eligible(con,row[0]): con.execute('DELETE FROM catalog_chunks_fts WHERE sha256=?',(row[0],))

def eligible(con,sha):
    for row in con.execute('SELECT metadata,category FROM catalog_occurrences WHERE sha256=?',(sha,)):
        a=json.loads(row['metadata']); kind=a.get('kind')
        if a.get('searchable') is False and kind!='service_event': continue
        setting=con.execute('SELECT enabled FROM index_policy WHERE kind=?',(kind,)).fetchone()
        if row['category']=='document' or setting is None or setting[0]: return True
    return False

def get(store):
    with store.connect() as con: return [dict(r) for r in con.execute('SELECT * FROM index_policy ORDER BY kind')]

def set_policy(store,kind,enabled,expected_version,actor):
    if kind not in KINDS or type(enabled) is not bool or not actor.strip(): raise ValueError('Valid payload kind, boolean and actor required')
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        current=con.execute('SELECT version FROM index_policy WHERE kind=?',(kind,)).fetchone()[0]
        if current!=expected_version: raise ValueError('Index policy version conflict')
        con.execute('UPDATE index_policy SET enabled=?,version=version+1 WHERE kind=?',(int(enabled),kind))
        from .catalog import _audit
        _audit(con,actor,'index_policy_changed',{'kind':kind,'enabled':enabled,'version':current+1})
        hashes=[r[0] for r in con.execute("SELECT DISTINCT sha256 FROM catalog_occurrences WHERE sha256 IS NOT NULL AND json_extract(metadata,'$.kind')=?",(kind,))]
        for sha in hashes:
            con.execute('DELETE FROM catalog_chunks_fts WHERE sha256=?',(sha,))
            if eligible(con,sha):
                con.execute('INSERT INTO catalog_chunks_fts SELECT sha256,position,text FROM catalog_chunks WHERE sha256=?',(sha,))
    if enabled:
        from .catalog import Catalog
        catalog=Catalog(store)
        for sha in hashes:
            with store.connect() as con:
                row=con.execute('SELECT b.size,f.name FROM catalog_blobs b JOIN catalog_occurrences o ON o.sha256=b.sha256 JOIN catalog_files f ON f.id=o.file_id WHERE b.sha256=? LIMIT 1',(sha,)).fetchone()
            if row: catalog._extract_text(sha,row['name'],row['size'])
    return {'policy':get(store),'affected_blobs':len(hashes),'originals_preserved':True,'physical_compaction':'not performed'}
