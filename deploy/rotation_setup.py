"""Install the renewal controller on the owner's VM using private SSH stdin.

Requires Cloud.ru Container Apps and Secret Management credentials. Prefer a
dedicated account limited to this project. Keys never enter output or the edge.
"""
import base64
import json
from pathlib import Path
import shlex
import subprocess

from setup import ROOT, INSTALL_BOOTSTRAP, read, save, ssh_args, command_env, checked

ALLOWED={'public_host','origin_ip','project_id','container_name','container_id','image',
         'origin_port','application_port','identity_port','origin_service','drain_seconds'}

REMOTE='''import sys,json,pathlib,base64,hashlib,subprocess
p=json.load(sys.stdin);root=pathlib.Path(p['root']).resolve();state=root/'gateway-rotation'
state.mkdir(exist_ok=True,mode=0o700)
source=state/'releases'/hashlib.sha256(json.dumps(p['files'],sort_keys=True).encode()).hexdigest()
source.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,text in p['files'].items():
 if not ((pathlib.Path(name).name==name and name.endswith('.py')) or name=='edge/entrypoint.sh'):raise ValueError('Invalid source name')
 (source/name).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 (source/name).write_text(text,encoding='utf-8');(source/name).chmod(0o600)
python=root/'oauth10/venv/bin/python'
if not python.exists():python=root/'current/venv/bin/python'
program="import sys,json;sys.path.insert(0,sys.argv[1]);from rotation_setup import configure_server;p=json.load(sys.stdin);print(json.dumps(configure_server(p)))"
r=subprocess.run([str(python),'-c',program,str(source)],input=json.dumps(p),text=True,capture_output=True)
if r.returncode:raise RuntimeError('Rotation installation failed; private diagnostics withheld')
sys.stdout.write(r.stdout)
'''


def configure_server(payload):
    import os,sys,tempfile
    from gateway_rotation import initialize, status, write, schedule, credentials_info
    from gateway_rotation_cloud import CloudBackend, desired_signature
    from setup import private_directory
    root=Path(payload['root']).resolve();state=private_directory(root/'gateway-rotation')
    with __import__('sqlite3').connect((root/'memory/context.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        actual=db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    if actual!=payload['workspace_id']:raise ValueError('Wrong memory host')
    options=payload['options']
    if set(options)-ALLOWED:raise ValueError('Unsupported rotation options')
    config={**options,'backend':'cloudru','caddy':str(root/'gateway10/caddy'),
        'origin_config':str(root/'gateway10/keys/origin.Caddyfile'),'cloud_state':str(state/'cloud')}
    private_directory(state/'cloud')
    credentials=state/'cloud/credentials.json'
    values=payload['credentials']
    if set(values)!={'AUTH_KEY_ID','AUTH_SECRET'} or not all(values.values()):raise ValueError('Missing cloud credentials')
    if credentials.exists() and read(credentials)!=values:raise ValueError('Cloud credentials differ; review explicit replacement')
    write(credentials,values)
    if not (state/'config.json').exists():
        with tempfile.TemporaryDirectory(prefix='.enroll-',dir=state) as tmp:
            generation=Path(tmp)
            for name in __import__('gateway_rotation').FILES:
                (generation/name).write_bytes(base64.b64decode(payload['generation'][name],validate=True));(generation/name).chmod(0o600)
            credentials_info(generation,options['public_host'])
            # Verify the seed still matches both the VM files and deployed edge.
            if (generation/'origin.pem').read_bytes()!=(root/'gateway10/keys/origin.pem').read_bytes():raise ValueError('Stale origin seed')
            from cloud_gateway import CloudAPI, public_origin
            api=CloudAPI(state/'cloud',options['project_id'])
            container=api.request('containers','/v2/containers/'+options['container_name'])
            if container['id']!=options['container_id'] or public_origin(container)!='https://'+options['public_host']:
                raise ValueError('Wrong edge container')
            env={e['name']:e for e in container['template']['containers'][0]['env']}
            if base64.b64decode(env['PC_EDGE_CERT_B64']['value'])!=(generation/'edge.pem').read_bytes():raise ValueError('Stale edge seed')
            initial=(root/'gateway10/keys/origin.Caddyfile').read_text()
            (state/'pre-rotation-origin.Caddyfile').write_text(initial);(state/'pre-rotation-origin.Caddyfile').chmod(0o600)
            initialize(state,config,generation)
    else:
        expected={**config,'format':'portable-context-rotation-1'}
        if read(state/'config.json')!=expected:raise ValueError('Existing controller settings differ')
    # Read-only API proof before scheduling; no revision is published during installation.
    CloudBackend(state).container()
    scheduled=schedule(state,sys.executable,Path(__file__).parent/'gateway_rotation.py')
    return {'installed':True,'controller_state':str(state),'script':str(Path(__file__).parent/'gateway_rotation.py'),
        'python':sys.executable,'schedule':scheduled,'status':status(state),'edge_changed':False}


def install(state,config,generation):
    from gateway_rotation import FILES, credentials_info
    state=Path(state).resolve();deployment=read(state/'deployment.json');installed=read(state/'installed.json')
    options=read(Path(config));generation=Path(generation).resolve()
    if set(options)-ALLOWED:raise ValueError('Unsupported rotation options')
    credentials_info(generation,options['public_host'])
    env=command_env(state)
    names=('rotation_setup.py','gateway_rotation.py','gateway_rotation_cloud.py','gateway_diagnostics.py','gateway.py','cloud_gateway.py','setup.py','edge/entrypoint.sh')
    payload={'root':installed['root'],'workspace_id':installed['memory']['workspace_id'],'options':options,
        'credentials':{k:env[k] for k in ('AUTH_KEY_ID','AUTH_SECRET')},
        'generation':{name:base64.b64encode((generation/name).read_bytes()).decode() for name in FILES},
        'files':{name:(ROOT/'deploy'/name).read_text(encoding='utf-8') for name in names}}
    result=checked(ssh_args(state,deployment)+['python3 -c '+shlex.quote(INSTALL_BOOTSTRAP)],
        input=json.dumps({'script':REMOTE,'payload':payload}),capture_output=True,text=True,encoding='utf-8')
    receipt=json.loads(result.stdout);save(state/'rotation-installed.json',receipt)
    return receipt
