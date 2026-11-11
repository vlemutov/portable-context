"""Reviewed Cloud.ru HTTPS/OAuth install and resume; no personal pilot scripts.

Cloud resources are addressed by saved IDs, not adopted by guessed names.
The existing VM/memory installer remains the only package migration path.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import uuid

from setup import ROOT, INSTALL_BOOTSTRAP, read, save, sha, private_directory, ssh_args, terraform, command_env
from cloud_gateway import CloudAPI, CloudAPIError, CADDY_IMAGE, container_request, public_origin
from gateway import issue_credentials, edge_config


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def plan(state, options=None):
    state=Path(state);deployment=read(state/'deployment.json');options=dict(options or {})
    allowed={'project_id','security_group_id','registry_id','container_id','container_name','image','origin_ip'}
    if set(options)-allowed:raise ValueError('Unsupported HTTPS options')
    project=str(uuid.UUID(options.get('project_id') or deployment.get('project_id','')))
    import ipaddress
    address=str(ipaddress.IPv4Address(options.get('origin_ip') or deployment.get('host','')))
    group=options.get('security_group_id')
    if not group:
        outputs=json.loads(terraform(state,['output','-json'],capture=True).stdout)
        group=outputs.get('security_group_id',{}).get('value')
        if not group:
            data=json.loads(terraform(state,['show','-json'],capture=True).stdout)
            group=next((r['values']['id'] for r in data.get('values',{}).get('root_module',{}).get('resources',[])
                if r['address']=='cloudru_evolution_compute_security_group.memory'),None)
    group=str(uuid.UUID(group or ''))
    name=options.get('container_name',deployment['name']+'-edge')
    if not re.fullmatch(r'[a-z][a-z0-9-]{3,48}[a-z0-9]',name):raise ValueError('Invalid edge name')
    config={'project_id':project,'origin_ip':address,'security_group_id':group,'container_name':name,
        **{k:options[k] for k in ('registry_id','container_id','image') if k in options}}
    for key in ('registry_id','container_id'):
        if key in config:config[key]=str(uuid.UUID(config[key]))
    if 'image' in config and not re.fullmatch(r'[a-z0-9-]+\.cr\.cloud\.ru/[a-z0-9/_-]+@sha256:[a-f0-9]{64}',config['image']):
        raise ValueError('Use a pinned image digest in the owner registry')
    folder=state/'https-firewall';folder.mkdir(exist_ok=True)
    import shutil
    shutil.copyfile(ROOT/'deploy/cloudru-edge/main.tf',folder/'main.tf')
    save(folder/'deployment.auto.tfvars.json',{'project_id':project,'security_group_id':group,'origin_port':9443})
    terraform(state,['init','-input=false','-no-color'],folder=folder,capture=True)
    terraform(state,['plan','-input=false','-no-color','-out=https.tfplan'],folder=folder,capture=True)
    data=json.loads(terraform(state,['show','-json','https.tfplan'],folder=folder,capture=True).stdout)
    from setup import summarize_plan
    changes=summarize_plan(data)
    if any(r['resource']!='cloudru_evolution_compute_security_group_rule.gateway_origin' for r in changes):
        raise ValueError('Unexpected HTTPS network change')
    reviewed={'format':1,'config':config,'deployment_sha256':sha(state/'deployment.json'),
        'firewall_plan_sha256':sha(folder/'https.tfplan'),'firewall_files_sha256':firewall_fingerprint(folder),
        'changes':changes,'upstream_image':CADDY_IMAGE,'scaling':{'min':0,'max':1},
        'costs':'Container runtime while active, private registry storage, Secret Management, traffic; existing VM/S3 costs remain. No live quote.',
        'pricing_url':'https://cloud.ru/calculator','access':'Public HTTPS requires owner OAuth; VM port 9443 requires mTLS.'}
    reviewed['plan_sha256']=digest(reviewed)
    existing=state/'https-journal.json'
    if existing.exists() and read(existing)['config']!=config:raise ValueError('Existing HTTPS deployment differs; separate migration required')
    save(state/'https-plan.json',reviewed)
    return reviewed


def firewall_fingerprint(folder):
    return digest({p.name:sha(p) for p in sorted(folder.iterdir()) if p.name.endswith(('.tf','.tfvars.json')) or p.name=='.terraform.lock.hcl'})


def approved_plan(state, approval):
    review=read(state/'https-plan.json');expected=review['plan_sha256']
    if digest({k:v for k,v in review.items() if k!='plan_sha256'})!=expected:raise ValueError('HTTPS plan changed')
    saved=read(state/'https-approved.json') if (state/'https-approved.json').exists() else {}
    if approval!=expected and saved.get('plan_sha256')!=expected:raise ValueError('Review HTTPS costs/access and pass --approve-plan '+expected)
    if sha(state/'deployment.json')!=review['deployment_sha256']:raise ValueError('Deployment changed; review a new HTTPS plan')
    folder=state/'https-firewall'
    if firewall_fingerprint(folder)!=review['firewall_files_sha256'] or sha(folder/'https.tfplan')!=review['firewall_plan_sha256']:
        raise ValueError('HTTPS firewall plan changed')
    save(state/'https-approved.json',{'plan_sha256':expected})
    return review


class Journal:
    def __init__(self,state,config):
        self.path=state/'https-journal.json'
        self.value=read(self.path) if self.path.exists() else {'format':1,'config':config,'completed':{},'intent':{}}
        if self.value['config']!=config:raise ValueError('HTTPS journal belongs to another target')
        self.save()
    def save(self):save(self.path,self.value)
    def stage(self,name,fn):
        if name in self.value['completed']:return self.value['completed'][name]
        self.value['stage']=name;self.value['intent'].setdefault(name,{'id':uuid.uuid4().hex});self.save()
        value=fn(self.value['intent'][name]);self.value['completed'][name]=value;self.save();return value


def wait_for(fn, ready, timeout=600):
    deadline=time.monotonic()+timeout
    while True:
        value=fn()
        if ready(value):return value
        if time.monotonic()>=deadline:raise ValueError('Cloud operation pending; rerun with the same state')
        time.sleep(5)


REMOTE='''import json,sys,pathlib,hashlib,subprocess,base64
p=json.load(sys.stdin);root=pathlib.Path(p['root']).resolve()
source=root/'https-tools'/hashlib.sha256(json.dumps(p['files'],sort_keys=True).encode()).hexdigest()
source.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,text in p['files'].items():
 path=source/name
 if not ((pathlib.Path(name).name==name and name.endswith('.py')) or name in ('edge/tool-lock.json','edge/entrypoint.sh')):raise ValueError('Invalid helper')
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_text(text,encoding='utf-8');path.chmod(0o600)
program="import sys,json;sys.path.insert(0,sys.argv[1]);from https_setup import server_action;print(json.dumps(server_action(json.load(sys.stdin))))"
r=subprocess.run([str(root/'current/venv/bin/python'),'-c',program,str(source)],input=json.dumps(p),capture_output=True,text=True)
if r.returncode:raise RuntimeError('HTTPS server step failed; credentials and private output withheld')
sys.stdout.write(r.stdout)
'''


def server_action(p):
    from https_runtime import configure, describe, metadata, workspace
    root=Path(p['root']);expected=p['workspace_id']
    if workspace(root)!=expected:raise ValueError('Wrong memory host')
    if p['action']=='configure':
        directory=root/'https-seed';directory.mkdir(exist_ok=True,mode=0o700)
        from gateway_rotation import FILES, credentials_info
        for name in FILES:
            data=base64.b64decode(p['generation'][name],validate=True);path=directory/name
            if path.exists() and path.read_bytes()!=data:raise ValueError('Initial generation changed')
            if not path.exists():path.write_bytes(data);path.chmod(0o600)
        credentials_info(directory,p['origin'].removeprefix('https://'))
        return configure(root,expected,p['origin'],directory)
    manifest=describe(root)
    if not manifest or manifest['origin']!=p['origin']:raise ValueError('Wrong HTTPS identity')
    if p['action']=='login':
        from oauth_recovery import owner_login
        output=root/('owner-login-'+uuid.uuid4().hex+'.json')
        try:owner_login(manifest['runtime'],output);return {'private_login':read(output)}
        finally:output.unlink(missing_ok=True)
    if p['action']=='check':return {'workspace_id':expected,**metadata(manifest)}
    if p['action']=='claude-code':
        from oauth_recovery import runtime_info, IdentityBackups
        from pocketid import Bootstrap
        _,env,receipt,config=runtime_info(manifest['runtime'])
        if config.issuer!=p['origin']:raise ValueError('Wrong OAuth issuer')
        backup=IdentityBackups(root/'identity-backup-config.json')
        if backup.runtime!=Path(manifest['runtime']).resolve() or backup.config.get('issuer')!=config.issuer:
            raise ValueError('Identity backup belongs to another runtime or issuer')
        result=Bootstrap(int(env['PORT']),env['STATIC_API_KEY']).enable_claude_code(receipt,backup.backup)
        return {'workspace_id':expected,**result}
    raise ValueError('Unsupported HTTPS server action')


def remote(state, action, origin, generation=None):
    installed=read(state/'installed.json');deployment=read(state/'deployment.json')
    names=('https_setup.py','https_runtime.py','gateway.py','gateway_rotation.py','gateway_diagnostics.py','pocketid.py','oauth_recovery.py',
        'setup.py','cloud_gateway.py','edge_tools.py','edge/tool-lock.json','edge/entrypoint.sh')
    p={'root':installed['root'],'workspace_id':installed['memory']['workspace_id'],'origin':origin,'action':action,
        'files':{name:(ROOT/'deploy'/name).read_text(encoding='utf-8') for name in names}}
    if generation:
        from gateway_rotation import FILES
        p['generation']={name:base64.b64encode((generation/name).read_bytes()).decode() for name in FILES}
    response=subprocess.run(ssh_args(state,deployment)+['python3 -c '+shlex.quote(INSTALL_BOOTSTRAP)],
        input=json.dumps({'script':REMOTE,'payload':p}),capture_output=True,text=True,timeout=600)
    if response.returncode:raise ValueError('HTTPS server step failed; retry same state; private response withheld')
    return json.loads(response.stdout)


def connection_receipt(state):
    """Adopt the verified endpoint of a lifecycle-updated pilot, without new identity."""
    state=Path(state);path=state/'https-installed.json'
    if path.exists():return read(path)
    installed=read(state/'installed.json');endpoint=installed.get('https') or {}
    if not endpoint.get('ready'):raise ValueError('Run update to recognize the existing HTTPS installation first')
    origin=endpoint['origin'];local=remote(state,'check',origin);public=check_public(origin)
    if local.get('workspace_id')!=installed['memory']['workspace_id']:raise ValueError('HTTPS memory mismatch')
    receipt={'installed':True,'origin':origin,'mcp_url':origin+'/mcp','workspace_id':local['workspace_id'],
        'client_id':'portable-context-desktop','callback_url':'http://127.0.0.1:14881/callback',
        'scopes':['openid','offline_access','memory:read','memory:write'],
        'checks':{'server':local,'public':public},'adopted_existing_installation':True}
    save(path,receipt);save_client(state,receipt)
    return receipt


def save_client(state,receipt):
    save(state/'client-https.json',{'mcpServers':{'portable-context':{'url':receipt['mcp_url']}},
        'oauth':{k:receipt[k] for k in ('client_id','callback_url','scopes')}})


def owner_login(state):
    state=Path(state);receipt=connection_receipt(state);result=remote(state,'login',receipt['origin'])
    path=state/('owner-login-'+uuid.uuid4().hex[:12]+'.json');save(path,result['private_login'])
    return {'private_file':str(path),'expires_in_minutes':20,'next':'Owner reads this private file locally, enters code and enrolls passkey. Never paste the code into chat.'}


def claude_code_client(state):
    """Enable the exact localhost callback on an existing owner installation."""
    state=Path(state);receipt=connection_receipt(state)
    result=remote(state,'claude-code',receipt['origin'])
    result.update(mcp_url=receipt['mcp_url'],scopes=['openid','offline_access','memory:read','memory:write'])
    save(state/'claude-code-client.json',result)
    return result


def check_public(origin, timeout=180):
    from gateway_diagnostics import probe
    import ssl
    host=origin.removeprefix('https://');context=ssl.create_default_context();deadline=time.monotonic()+timeout
    while True:
        results=[probe(host,443,host,kind,context=context) for kind in ('transport','metadata','challenge')]
        if all(r['ok'] for r in results):return {'transport':True,'oauth_metadata':True,'unauthenticated_rejected':True}
        if time.monotonic()>deadline:raise ValueError('Public HTTPS not ready; retry the same installation without recreating resources')
        time.sleep(3)


def install(state, approval=None):
    state=private_directory(state);review=approved_plan(state,approval);config=review['config']
    # Cross-platform nonblocking lock; credentials remain on the owner machine.
    from portable_context.restic_backup import file_lock
    with file_lock(state/'https-install.lock'):
        return install_locked(state,config)


def install_locked(state,config):
    from setup import install as install_memory
    from rotation_setup import install as install_rotation
    journal=Journal(state,config);folder=private_directory(state/'https')
    # Always run the package updater: a completed cloud journal is not proof of current code.
    memory=install_memory(state)
    machine=subprocess.run(ssh_args(state,read(state/'deployment.json'))+['uname -m'],capture_output=True,text=True,timeout=30)
    if machine.returncode or machine.stdout.strip() not in ('x86_64','amd64'):
        raise ValueError('Automatic HTTPS identity profile requires a Linux AMD64 server; no edge resources created')
    api=CloudAPI(state,config['project_id']);name=config['container_name']
    def registry(intent):
        resource=config.get('registry_id') or intent.get('resource_id')
        if not resource:
            if intent.get('submitted'):raise ValueError('Registry creation reply was lost; use https-reconcile --stage registry --resource-id REVIEWED_ID, then retry with the same state')
            existing=api.request('registry','/v1/registries').get('registries',[])
            if any(r['name']==name for r in existing):raise ValueError('Registry name occupied; explicitly review its ID or choose another name')
            intent['submitted']=True;journal.save()
            operation=api.request('registry','/v1/registries',method='POST',body={'projectId':config['project_id'],'name':name,'isPublic':False,'registryType':'DOCKER','tariff':'BASIC'})
            resource=operation['resourceId'];intent['resource_id']=resource;journal.save()
        value=wait_for(lambda:api.request('registry','/v1/registries/'+resource),lambda v:v.get('status')=='ACTIVE')
        if value.get('isPublic') or value.get('registryType')!='DOCKER':raise ValueError('Expected private Docker registry')
        return {'id':resource,'name':value['name']}
    reg=journal.stage('registry',registry)
    verified_registry=api.request('registry','/v1/registries/'+reg['id'])
    if verified_registry.get('id')!=reg['id'] or verified_registry.get('name')!=reg['name'] or verified_registry.get('isPublic') or verified_registry.get('registryType')!='DOCKER':
        raise ValueError('Recorded private registry changed')
    def image(intent):
        if config.get('image'):
            if not config['image'].startswith(reg['name']+'.cr.cloud.ru/'):
                raise ValueError('Image must belong to the reviewed private registry')
            return {'image':config['image']}
        from edge_tools import install as tool
        crane=tool('crane',state/'tools');host=reg['name']+'.cr.cloud.ru';target=host+'/edge-runtime:caddy-2.11.9'
        values=command_env(state)
        with tempfile.TemporaryDirectory(prefix='registry-auth-',dir=folder) as temporary:
            auth=private_directory(temporary)
            save(auth/'config.json',{'auths':{host:{'auth':base64.b64encode((values['AUTH_KEY_ID']+':'+values['AUTH_SECRET']).encode()).decode()}}})
            env={**os.environ,'DOCKER_CONFIG':str(auth)}
            for args in ([str(crane),'copy','--platform','linux/amd64',CADDY_IMAGE,target],[str(crane),'digest',target]):
                response=subprocess.run(args,env=env,capture_output=True,text=True,timeout=600)
                if response.returncode:raise ValueError('Pinned registry image transfer failed; private output suppressed')
            value=response.stdout.strip()
            if not re.fullmatch(r'sha256:[a-f0-9]{64}',value):raise ValueError('Invalid image digest')
        return {'image':host+'/edge-runtime@'+value}
    image_value=journal.stage('image',image)['image']
    def container(intent):
        resource=config.get('container_id') or intent.get('resource_id')
        if not resource:
            if intent.get('submitted'):raise ValueError('Container creation reply was lost; use https-reconcile --stage container --resource-id REVIEWED_ID, then retry with the same state')
            try:api.request('containers','/v2/containers/'+name)
            except CloudAPIError as error:
                if error.status!=404:raise
            else:raise ValueError('Container name occupied; explicitly select its ID')
            body=container_request(config['project_id'],name,image_value,config=b'',certificate=b'',ca=b'',key_secret='pending')
            app=body['template']['containers'][0];app.update(command=['caddy'],args=['respond','--listen',':8080','--body','Portable Context setup pending','--status','503'],env=[])
            intent['submitted']=True;journal.save()
            operation=api.request('containers','/v2/containers',method='POST',body=body)
            resource=operation['resourceId'];intent['resource_id']=resource;journal.save()
        value=wait_for(lambda:api.request('containers','/v2/containers/'+name),lambda v:v.get('status')=='running')
        if value['id']!=resource or value.get('projectId')!=config['project_id']:raise ValueError('Container ownership mismatch')
        validate_placeholder(value,image_value)
        return {'id':resource,'origin':public_origin(value)}
    edge=journal.stage('container',container);origin=edge['origin'];host=origin.removeprefix('https://')
    # Validate identity on every rerun, even if all creation receipts are complete.
    current=api.request('containers','/v2/containers/'+name)
    if current['id']!=edge['id'] or current.get('projectId')!=config['project_id'] or public_origin(current)!=origin:
        raise ValueError('HTTPS endpoint changed')
    if current['template']['containers'][0]['image']!=image_value:raise ValueError('Container image changed outside installer')
    generation=folder/'generation'
    if not generation.exists():
        with tempfile.TemporaryDirectory(prefix='seed-',dir=folder) as tmp:
            candidate=Path(tmp)/'generation';issue_credentials(candidate,host);candidate.rename(generation)
    server=remote(state,'configure',origin,generation)
    def firewall(intent):
        # Listener is authenticated before the reviewed public ingress is applied.
        folder=state/'https-firewall'
        if (folder/'terraform.tfstate').exists():
            existing=json.loads(terraform(state,['show','-json'],folder=folder,capture=True).stdout)
            resources=existing.get('values',{}).get('root_module',{}).get('resources',[])
            rules=[r['values'] for r in resources if r['address']=='cloudru_evolution_compute_security_group_rule.gateway_origin']
            if rules:
                rule=rules[0]
                if rule['security_group_id']!=config['security_group_id'] or rule['port_range']!='9443:9443' or rule['remote_ip_prefix']!='0.0.0.0/0':
                    raise ValueError('Existing HTTPS firewall differs from the reviewed rule')
                return {'applied':True,'resumed_from_state':True}
        terraform(state,['apply','-input=false','-no-color','https.tfplan'],folder=state/'https-firewall',capture=True)
        return {'applied':True}
    journal.stage('firewall',firewall)
    def publish(intent):
        secret=name+'-initial-key';expected=base64.b64encode((generation/'edge-key.pem').read_bytes())
        try:value=api.request('secrets','/v2/version/'+secret)
        except CloudAPIError as error:
            if error.status!=404:raise
            api.request('secrets','/v2/secret',method='POST',body={'projectId':config['project_id'],'path':secret,
                'description':'Portable Context transport key','payload':base64.b64encode(expected).decode()})
            value=api.request('secrets','/v2/version/'+secret)
        if base64.b64decode(value['payload'],validate=True)!=expected:raise ValueError('Existing secret belongs to another generation')
        body=container_request(config['project_id'],name,image_value,config=edge_config(host,config['origin_ip'],revision='initial').encode(),
            certificate=(generation/'edge.pem').read_bytes(),ca=(generation/'origin-ca.pem').read_bytes(),key_secret=secret)
        body.pop('metadata',None);body['configuration'].pop('privileged',None)
        from gateway_rotation_cloud import desired_signature
        current=api.request('containers','/v2/containers/'+name)
        actual=desired_signature(current);wanted=desired_signature(body)
        if actual!=wanted:
            if intent.get('submitted'):raise ValueError('Publication still unresolved; inspect the same cloud revision before retrying')
            intent['submitted']=True;journal.save()
            api.request('containers','/v2/containers/'+name,method='PATCH',body=body)
        wait_for(lambda:api.request('containers','/v2/containers/'+name),lambda v:v.get('status')=='running')
        check_public(origin,600)
        return {'published':True}
    journal.stage('publish',publish)
    rotation={'public_host':host,'origin_ip':config['origin_ip'],'project_id':config['project_id'],
        'container_name':name,'container_id':edge['id'],'image':image_value,'origin_port':9443,'application_port':8767,
        'identity_port':14120,'origin_service':server['origin_service']}
    save(folder/'rotation-options.json',rotation)
    renewal=install_rotation(state,folder/'rotation-options.json',generation)
    from setup import recovery_kit
    kit=recovery_kit(state,server['runtime'])
    local=remote(state,'check',origin);public=check_public(origin)
    receipt={'installed':True,'origin':origin,'mcp_url':origin+'/mcp','workspace_id':memory['workspace_id'],
        'client_id':'portable-context-desktop','callback_url':'http://127.0.0.1:14881/callback',
        'scopes':['openid','offline_access','memory:read','memory:write'],'rotation':renewal['schedule'],
        'recovery_kit':kit,'checks':{'server':local,'public':public},'owner_enrollment':'required on first install'}
    save(state/'https-installed.json',receipt)
    save_client(state,receipt)
    # First handoff only; update must not create fresh owner login credentials.
    if not (state/'owner-enrollment.json').exists():
        handoff=owner_login(state);save(state/'owner-enrollment.json',handoff)
    journal.value['stage']='complete';journal.save()
    return {**receipt,'client_config':str(state/'client-https.json'),'owner_login':str(state/'owner-enrollment.json')}


def validate_placeholder(value,image):
    containers=value.get('template',{}).get('containers',[])
    if len(containers)!=1:raise ValueError('Expected a single setup placeholder container')
    app=containers[0]
    if (app.get('image')!=image or app.get('command')!=['caddy'] or app.get('env')!=[] or
        app.get('args')!=['respond','--listen',':8080','--body','Portable Context setup pending','--status','503']):
        raise ValueError('Existing container is not the setup placeholder; refusing to replace another application')


def reconcile(state,stage,resource_id):
    """Explicit adoption after an ambiguous CREATE; never create/delete resources."""
    from portable_context.restic_backup import file_lock
    state=Path(state)
    with file_lock(state/'https-install.lock'):
        return reconcile_locked(state,stage,resource_id)


def reconcile_locked(state,stage,resource_id):
    state=Path(state);review=approved_plan(state,None);config=review['config']
    resource_id=str(uuid.UUID(resource_id));journal=Journal(state,config)
    intent=journal.value['intent'].get(stage,{})
    if stage not in ('registry','container') or not intent.get('submitted') or stage in journal.value['completed']:
        raise ValueError('No ambiguous resource creation to reconcile')
    api=CloudAPI(state,config['project_id']);name=config['container_name']
    if stage=='registry':
        value=api.request('registry','/v1/registries/'+resource_id)
        if value.get('id')!=resource_id or value.get('name')!=name or value.get('isPublic') or value.get('registryType')!='DOCKER':raise ValueError('Registry does not match reviewed intent')
    else:
        value=api.request('containers','/v2/containers/'+name)
        if value['id']!=resource_id or value.get('projectId')!=config['project_id']:raise ValueError('Container does not match reviewed intent')
        validate_placeholder(value,journal.value['completed']['image']['image'])
    intent['resource_id']=resource_id;journal.save()
    return {'reconciled':stage,'resource_id':resource_id,'next':'Repeat install --https with the same state'}
