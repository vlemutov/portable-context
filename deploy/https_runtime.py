"""Linux HTTPS lifecycle, shared by first install and package updates.

Identity and transport state are persistent; executable code follows current.
Only receipts leave the server. Bootstrap credentials never enter service argv.
"""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

from gateway import hostname, origin_config, port
from pocketid import Bootstrap, VERSION, LINUX_AMD64_SHA256
from setup import save, read


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def write(path, text, mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    pending=path.with_suffix(path.suffix+'.new');pending.write_text(text,encoding='utf-8')
    pending.chmod(mode);pending.replace(path)


def workspace(root):
    with closing(sqlite3.connect((Path(root)/'memory/context.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        return db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]


def quoted(value):
    value=str(value)
    if any(c in value for c in ('%', '\n', '\r', '"', '\\')):raise ValueError('Unsupported systemd path')
    return '"'+value+'"'


def unit_file(name):
    import re
    if not re.fullmatch(r'pc-[a-zA-Z0-9_-]+\.service',name):raise ValueError('Invalid owned service')
    return Path.home()/'.config/systemd/user'/name


def service(name, argv, owned_root, *, requires=None):
    path=unit_file(name)
    if path.exists() and str(owned_root) not in path.read_text():
        raise ValueError('Service belongs to another installation')
    dependencies=('After='+requires+'\nRequires='+requires+'\n') if requires else ''
    write(path,'[Unit]\nDescription=Portable Context managed HTTPS\n'+dependencies+
        '[Service]\nExecStart='+' '.join(quoted(x) for x in argv)+
        '\nRestart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=yes\n'
        'StandardOutput=null\nStandardError=null\n[Install]\nWantedBy=default.target\n')
    run(['systemctl','--user','daemon-reload'])
    run(['systemctl','--user','enable',name])


def metadata(manifest, timeout=60):
    origin=manifest['origin'];host=origin.removeprefix('https://')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline=time.monotonic()+timeout
    while True:
        try:
            request=urllib.request.Request('http://127.0.0.1:'+str(manifest['application_port'])+
                '/.well-known/oauth-protected-resource/mcp',headers={'Host':host})
            with opener.open(request,timeout=3) as response:value=json.load(response)
            if value.get('resource')!=origin+'/mcp' or value.get('authorization_servers')!=[origin]:
                raise ValueError('MCP readiness returned another identity')
            try:
                opener.open(urllib.request.Request('http://127.0.0.1:'+str(manifest['application_port'])+'/mcp',
                    headers={'Host':host}),timeout=3).close()
            except urllib.error.HTTPError as error:
                if error.code==401:return {'metadata':True,'unauthenticated_rejected':True}
                raise
            raise ValueError('MCP accepted an unauthenticated readiness request')
        except (OSError,ValueError):
            if time.monotonic()>=deadline:raise ValueError('OAuth MCP not ready; rerun the same installation') from None
            time.sleep(.25)


def describe(root):
    """Recognize managed installations and the bound 1.0 pilot; never guess an owner."""
    root=Path(root).resolve();path=root/'https-runtime.json'
    if path.exists():
        result=read(path)
        if result['workspace_id']!=workspace(root):raise ValueError('HTTPS workspace mismatch')
        if Path(result['runtime']).resolve().parent!=root:raise ValueError('HTTPS runtime outside installation')
        for key in ('identity_service','mcp_service','origin_service'):
            if key not in result:continue
            unit=unit_file(result[key])
            if unit.exists() and str(root) not in unit.read_text():raise ValueError('Service belongs to another installation')
        return result
    candidates=[root/name for name in ('oauth','oauth10') if (root/name/'oauth.json').exists()]
    if not candidates:return None
    if len(candidates)!=1:raise ValueError('Ambiguous OAuth runtime; no update performed')
    runtime=candidates[0];config=read(runtime/'oauth.json');env=read(runtime/'identity/environment.json')
    receipt=read(runtime/'identity/receipt.json')
    if config['issuer']!=env['APP_URL'] or receipt['owner_subject']!=config['owner_subject']:
        raise ValueError('OAuth identity mismatch')
    manifest={'format':1,'runtime':str(runtime),'workspace_id':workspace(root),'origin':config['issuer'],
        'identity_port':int(env['PORT']),'application_port':8767,'origin_port':9443,
        'identity_service':'pc-identity.service','mcp_service':'pc-mcp-http.service',
        'origin_service':'pc-gateway-origin.service'}
    for name in ('identity_service','mcp_service','origin_service'):
        path=unit_file(manifest[name])
        if not path.exists() or str(root) not in path.read_text():raise ValueError('Unrecognized pilot service')
    return manifest


def update_manifest(root):
    """A bootstrap intent is not yet an installed HTTP writer.

    Configuration is persisted before the unit. A missing unit can safely be
    finished by configure; an existing unit without its binding is corruption.
    """
    manifest=describe(root)
    if not manifest:return None
    unit=unit_file(manifest['mcp_service'])
    if not unit.exists():return None
    if not (Path(manifest['runtime'])/'oauth.json').exists():
        raise ValueError('Existing HTTPS service lost its OAuth binding; restore it before updating')
    return manifest


def quiesce(root):
    manifest=update_manifest(root)
    if manifest:
        # Save intent before stopping, so retry and a killed installer retain ownership.
        save(Path(root)/'https-runtime.json',manifest)
        run(['systemctl','--user','stop',manifest['mcp_service']])
    return manifest


def start_mcp(root, manifest):
    root=Path(root);runtime=Path(manifest['runtime']);python=root/'current/venv/bin/python'
    config=read(runtime/'oauth.json')
    if config['issuer']!=manifest['origin'] or config['resource']!=manifest['origin']+'/mcp':
        raise ValueError('OAuth origin changed during update')
    args=[str(python),'-m','portable_context','--home',str(root/'memory'),'mcp',
        '--transport','streamable-http','--port',str(manifest['application_port']),
        '--source-root',str(root/'imports'),'--oauth-config',str(runtime/'oauth.json')]
    guard='if [ -e '+shlex.quote(str(root/'maintenance.json'))+' ]; then exit 75; fi; exec '+shlex.join(args)
    attachment=('export PC_ATTACHMENT_S3_CONFIG='+shlex.quote(str(root/'backup-config.json'))+'\n') if (root/'backup-config.json').exists() else ''
    write(runtime/'run-mcp','#!/bin/sh\n'+attachment+'exec flock -s -n '+shlex.quote(str(root/'service.lock'))+
        ' sh -c '+shlex.quote(guard)+'\n',0o700)
    try:
        service(manifest['mcp_service'],[runtime/'run-mcp'],root,requires=manifest['identity_service'])
        run(['systemctl','--user','restart',manifest['mcp_service']])
        return metadata(manifest)
    except Exception:
        # Close new writers even if systemd itself is unavailable.
        save(root/'maintenance.json',{'operation':'https-readiness','release':(root/'current').resolve().name,
            'checkpoint_complete':True,'next':'retry installer with the same source release'})
        try:run(['systemctl','--user','stop',manifest['mcp_service']])
        except Exception:pass
        raise


def download_provider(path):
    if platform.machine().lower() not in ('x86_64','amd64'):
        raise ValueError('Pinned identity installer currently requires Linux AMD64 server')
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=LINUX_AMD64_SHA256:raise ValueError('Unexpected identity binary')
        return
    with urllib.request.urlopen(f'https://github.com/pocket-id/pocket-id/releases/download/v{VERSION}/pocket-id_linux_amd64',timeout=120) as response:data=response.read()
    if hashlib.sha256(data).hexdigest()!=LINUX_AMD64_SHA256:raise ValueError('Identity download checksum mismatch')
    pending=path.with_suffix('.new');pending.write_bytes(data);pending.chmod(0o700);pending.replace(path)


def configure(root, expected_workspace, origin, generation, *, caddy=None, provider=None, ports=None):
    """Idempotently prepare private identity/MCP/origin before publishing the edge."""
    root=Path(root).resolve();host=hostname(origin.removeprefix('https://'))
    if origin!='https://'+host or workspace(root)!=expected_workspace:raise ValueError('Installation identity mismatch')
    ports=ports or {'identity_port':14120,'application_port':8767,'origin_port':9443}
    for value in ports.values():port(value)
    if len(set(ports.values())|{ports['identity_port']+1})!=4:raise ValueError('Service ports must be distinct')
    manifest=describe(root)
    if manifest:
        if manifest['origin']!=origin or any(manifest[k]!=v for k,v in ports.items()):raise ValueError('Existing HTTPS endpoint differs; migration required')
    else:
        prefix='pc' if root==Path.home()/'.local/share/portable-context-server' else 'pc-'+hashlib.sha256(str(root).encode()).hexdigest()[:12]
        manifest={'format':1,'workspace_id':expected_workspace,'origin':origin,'runtime':str(root/'oauth10'),**ports,
            'identity_service':prefix+'-identity.service','mcp_service':prefix+'-mcp-http.service','origin_service':prefix+'-gateway-origin.service'}
        # Persist full intent before creating identity or starting any service.
        save(root/'https-runtime.json',manifest)
    runtime=Path(manifest['runtime']);auth=runtime/'identity';auth.mkdir(parents=True,exist_ok=True,mode=0o700)
    if provider:
        data=Path(provider).read_bytes()
        if hashlib.sha256(data).hexdigest()!=LINUX_AMD64_SHA256:raise ValueError('Unpinned identity provider')
        if not (auth/'pocket-id').exists():(auth/'pocket-id').write_bytes(data);(auth/'pocket-id').chmod(0o700)
    download_provider(auth/'pocket-id')
    environment=auth/'environment.json'
    if not environment.exists():
        save(environment,{'APP_URL':origin,'HOST':'127.0.0.1','PORT':str(ports['identity_port']),
            'ACTORS_HOST':'127.0.0.1','ACTORS_PORT':str(ports['identity_port']+1),
            'UI_CONFIG_DISABLED':'true','ALLOW_USER_SIGNUPS':'disabled','REQUIRE_USER_EMAIL':'false',
            'ENCRYPTION_KEY':secrets.token_urlsafe(48),'STATIC_API_KEY':secrets.token_urlsafe(48),
            'ANALYTICS_DISABLED':'true','VERSION_CHECK_DISABLED':'true','ICON_LIBRARY_URL':'disabled',
            'LOG_LEVEL':'error','LOG_QUERY_ARGS':'false','CIMD_URL_ALLOWLIST':'[]'})
    env=read(environment)
    if env.get('APP_URL')!=origin or env.get('HOST')!='127.0.0.1' or env.get('ALLOW_USER_SIGNUPS')!='disabled':
        raise ValueError('Identity profile differs')
    write(auth/'run.py',"import os,json,pathlib\np=pathlib.Path(__file__).parent\nos.chdir(p)\ne={k:os.environ[k] for k in ('PATH','HOME','USER','LANG') if k in os.environ}\ne.update(json.loads((p/'environment.json').read_text()))\nos.execve(str(p/'pocket-id'),[str(p/'pocket-id')],e)\n")
    service(manifest['identity_service'],['/usr/bin/python3',auth/'run.py'],root)
    run(['systemctl','--user','start',manifest['identity_service']])
    api=Bootstrap(ports['identity_port'],env['STATIC_API_KEY'])
    for attempt in range(120):
        try:api.request('/api/users');break
        except (OSError,RuntimeError):
            if attempt==119:raise ValueError('Private identity service not ready') from None
            time.sleep(.5)
    receipt=api.configure(origin,auth/'receipt.json')
    desired={'issuer':origin,'resource':origin+'/mcp','provider_origin':'http://127.0.0.1:'+str(ports['identity_port']),'owner_subject':receipt['owner_subject']}
    if (runtime/'oauth.json').exists() and read(runtime/'oauth.json')!=desired:raise ValueError('Existing owner binding differs')
    save(runtime/'oauth.json',desired)
    ready=start_mcp(root,manifest)
    gateway=root/'gateway10';keys=gateway/'keys';keys.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (gateway/'caddy').exists():
        from edge_tools import install
        import shutil
        binary=Path(caddy) if caddy else install('caddy',root/'tools')
        shutil.copyfile(binary,gateway/'caddy');(gateway/'caddy').chmod(0o700)
    # A configured rotation owns its live configuration and all generations.
    if not (root/'gateway-rotation/config.json').exists():
        for name in ('origin.pem','origin-key.pem','edge-ca.pem'):
            data=Path(generation,name).read_bytes();target=keys/name
            if target.exists() and target.read_bytes()!=data:raise ValueError('Refusing to replace existing transport keys')
            if not target.exists():target.write_bytes(data);target.chmod(0o600)
        content=origin_config(host,keys,ports['origin_port'],ports['application_port'],ports['identity_port'],revision='initial')
        candidate=keys/'candidate.Caddyfile';write(candidate,content)
        run([str(gateway/'caddy'),'validate','--config',str(candidate),'--adapter','caddyfile'])
        write(keys/'origin.Caddyfile',content)
    service(manifest['origin_service'],[gateway/'caddy','run','--config',keys/'origin.Caddyfile','--adapter','caddyfile'],root)
    # start is a no-op for an already running origin; updates never restart it.
    run(['systemctl','--user','start',manifest['origin_service']])
    from gateway_diagnostics import probe,vm_context
    import ssl
    deadline=time.monotonic()+60
    while True:
        if (root/'gateway-rotation/config.json').exists():context=vm_context(root/'gateway-rotation')
        else:
            context=ssl.create_default_context(cafile=str(Path(generation)/'origin-ca.pem'))
            context.load_cert_chain(Path(generation)/'edge.pem',Path(generation)/'edge-key.pem')
        if probe('127.0.0.1',ports['origin_port'],host,'transport',context=context)['ok']:break
        if time.monotonic()>deadline:raise ValueError('Authenticated origin not ready; public ingress must remain closed')
        time.sleep(.25)
    return {'configured':True,'workspace_id':expected_workspace,'origin':origin,'runtime':str(runtime),
        'origin_service':manifest['origin_service'],'readiness':{**ready,'origin_mtls':True},'owner_subject':receipt['owner_subject']}
