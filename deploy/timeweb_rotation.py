"""Timeweb publication adapter for the existing overlapping-certificate controller."""
import base64
import json
from pathlib import Path
import shlex
import tempfile
import urllib.request

from gateway_rotation_cloud import CloudBackend, LocalOrigin
from cloud_gateway import NoRedirect
from timeweb_api import API, origin
from timeweb_setup import edge_env, publish
from setup import ROOT, INSTALL_BOOTSTRAP, read, save, private_directory, checked, ssh_args


class TimewebBackend(CloudBackend):
    def __init__(self,state):
        state=Path(state);config=read(state/'config.json')
        if config.get('backend')!='timeweb':raise ValueError('Expected Timeweb renewal configuration')
        LocalOrigin.__init__(self,state,config)
        self.api=API(config['cloud_state']);self.opener=urllib.request.build_opener(NoRedirect())

    def container(self):
        value=self.api.request('/api/v1/apps/'+str(self.config['app_id']))['app']
        if value.get('id')!=self.config['app_id'] or value.get('name')!=self.config['app_name'] or origin(value)!='https://'+self.config['public_host']:
            raise ValueError('Timeweb edge identity changed')
        if value.get('commit_sha')!=self.config['source_commit']:raise ValueError('Timeweb edge source changed outside installer')
        return value

    def edge(self,certificate,trusts,revision,journal):
        from gateway_rotation import materialize
        app=self.container();bundle=materialize(self.state,revision,certificate,trusts,'edge')
        env=edge_env(self.config['public_host'],self.config['origin_ip'],bundle,revision)
        if not publish(self.api,app,env,self.config['source_commit'],self.state/('publish-'+revision+'.json')):return False
        try:return self.public_probe(revision)
        except (OSError,ValueError):return False


def configure_server(payload):
    import sqlite3,sys
    from gateway_rotation import FILES, initialize, credentials_info, schedule, status, write
    from rotation_setup import ALLOWED
    root=Path(payload['root']).resolve();state=private_directory(root/'gateway-rotation')
    with sqlite3.connect((root/'memory/context.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        actual=db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    if actual!=payload['workspace_id']:raise ValueError('Wrong memory host')
    options=payload['options']
    if set(options)-ALLOWED:raise ValueError('Unsupported renewal options')
    config={**options,'backend':'timeweb','caddy':str(root/'gateway10/caddy'),
        'origin_config':str(root/'gateway10/keys/origin.Caddyfile'),'cloud_state':str(state/'cloud')}
    private_directory(state/'cloud');values=payload['credentials'];path=state/'cloud/credentials.json'
    if set(values)!={'TIMEWEB_TOKEN'} or not values['TIMEWEB_TOKEN']:raise ValueError('Missing Timeweb token')
    if path.exists() and read(path)!=values:raise ValueError('Renewal credentials changed. Explicit replacement required')
    write(path,values)
    if not (state/'config.json').exists():
        with tempfile.TemporaryDirectory(prefix='.enroll-',dir=state) as tmp:
            generation=Path(tmp)
            for name in FILES:
                (generation/name).write_bytes(base64.b64decode(payload['generation'][name],validate=True));(generation/name).chmod(0o600)
            credentials_info(generation,options['public_host'])
            if (generation/'origin.pem').read_bytes()!=(root/'gateway10/keys/origin.pem').read_bytes():raise ValueError('Stale origin seed')
            app=API(state/'cloud').request('/api/v1/apps/'+str(options['app_id']))['app']
            if app.get('name')!=options['app_name'] or origin(app)!='https://'+options['public_host'] or app.get('commit_sha')!=options['source_commit']:
                raise ValueError('Wrong Timeweb edge')
            env=app.get('envs',{})
            if base64.b64decode(env.get('PC_EDGE_CERT_B64',''),validate=True)!=(generation/'edge.pem').read_bytes():raise ValueError('Stale edge seed')
            (state/'pre-rotation-origin.Caddyfile').write_bytes((root/'gateway10/keys/origin.Caddyfile').read_bytes())
            (state/'pre-rotation-origin.Caddyfile').chmod(0o600)
            initialize(state,config,generation)
    elif read(state/'config.json')!={**config,'format':'portable-context-rotation-1'}:
        raise ValueError('Renewal settings differ')
    TimewebBackend(state).container()
    scheduled=schedule(state,sys.executable,Path(__file__).parent/'gateway_rotation.py')
    return {'installed':True,'schedule':scheduled,'controller_state':str(state),'status':status(state),'edge_changed':False}


def install(state,options,generation):
    from rotation_setup import REMOTE, source_names
    from gateway_rotation import FILES, credentials_info
    state=Path(state);installed=read(state/'installed.json');credentials_info(generation,options['public_host'])
    values=read(state/'credentials.json')
    payload={'root':installed['root'],'workspace_id':installed['memory']['workspace_id'],'options':options,
        'credentials':{'TIMEWEB_TOKEN':values['TIMEWEB_TOKEN']},
        'generation':{name:base64.b64encode((generation/name).read_bytes()).decode() for name in FILES},
        'files':{name:(ROOT/'deploy'/name).read_text(encoding='utf-8') for name in source_names()}}
    response=checked(ssh_args(state,read(state/'deployment.json'))+['python3 -c '+shlex.quote(INSTALL_BOOTSTRAP)],
        input=json.dumps({'script':REMOTE,'payload':payload}),capture_output=True,text=True,encoding='utf-8')
    result=json.loads(response.stdout);save(state/'rotation-installed.json',result);return result
