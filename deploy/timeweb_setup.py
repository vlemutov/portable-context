"""Owner-operated Timeweb installation. IDs and write intents survive interruption.

No automatic destruction. A missing creation reply requires explicit reconciliation.
The stateless App Platform gateway does not contain memory or cloud API credentials.
"""
import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from setup import ROOT, read, save, sha, private_directory, ensure_key
from timeweb_api import API, TimewebError, integer, origin


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def target(config):
    return {k:v for k,v in config.items() if k!='host'}


def validate(config):
    for key in ('server_preset_id','storage_preset_id','app_preset_id','os_id'):
        if type(config[key]) is not int:raise ValueError(key+' must be a JSON integer')
        integer(config[key])
    if config.get('project_id') is not None:integer(config['project_id'])
    if not re.fullmatch(r'[a-z0-9-]{2,30}',config.get('availability_zone','')):
        raise ValueError('Select an explicit availability_zone from the locations catalog')
    network=ipaddress.ip_network(config['ssh_cidr'],strict=True)
    if network.version!=4 or network.prefixlen<8:raise ValueError('Use an explicit SSH IPv4 CIDR')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,62}',config['bucket']):raise ValueError('Invalid bucket name')
    if config.get('ssh_user')!='pcadmin' or config.get('ssh_port',22)!=22:
        raise ValueError('Managed Timeweb VM uses pcadmin on SSH port 22')
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',config['source_url']):
        raise ValueError('Use a public GitHub repository without credentials')
    if not re.fullmatch(r'[a-f0-9]{40}',config['source_commit']):raise ValueError('Pin the source to a full published Git commit')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+',config['source_branch']):raise ValueError('Invalid source branch')
    if not 60<=int(config.get('dns_timeout',1800))<=3600:raise ValueError('DNS timeout must be 60..3600 seconds')


def catalog(state):
    api=API(state)
    # These endpoints contain product metadata, not credentials or account resources.
    return {name:api.request(path) for name,path in (
        ('servers','/api/v1/presets/servers'),('storage','/api/v1/presets/storages'),
        ('apps','/api/v1/presets/apps'),('operating_systems','/api/v1/os/servers'),('locations','/api/v2/locations'))}


def find_row(value, ident):
    if isinstance(value,dict):
        if value.get('id')==ident:return value
        for item in value.values():
            found=find_row(item,ident)
            if found is not None:return found
    if isinstance(value,list):
        for item in value:
            found=find_row(item,ident)
            if found is not None:return found
    return None


def plan(state,*,config=None,persist=True):
    config=read(state/'deployment.json') if config is None else config;validate(config)
    catalogs=catalog(state);prices={}
    for kind,key in (('servers','server_preset_id'),('storage','storage_preset_id'),('apps','app_preset_id')):
        row=find_row(catalogs[kind],integer(config[key]))
        if row is None:raise ValueError('Selected '+kind+' preset not in current catalog')
        prices[kind]={k:row[k] for k in ('id','price','location','cpu','ram','disk','cpu_frequency','disk_type','bandwidth','tags','description_short') if k in row}
        if 'location' in row and not str(row['location']).startswith(('ru','msk','spb')):
            raise ValueError('Select a Russian location for '+kind)
        if kind=='servers' and (float(row.get('ram',0))<2048 or float(row.get('disk',0))<20480):
            raise ValueError('Select a VM with at least 2 GiB RAM and 20 GiB disk')
        if kind=='servers':
            locations=catalogs['locations'].get('locations',[])
            if not any(v.get('location')==row.get('location') and config['availability_zone'] in v.get('availability_zones',[]) for v in locations):
                raise ValueError('VM availability zone does not match the selected preset location')
    operating_system=find_row(catalogs['operating_systems'],integer(config['os_id']))
    if operating_system is None:raise ValueError('OS not in catalog')
    if 'ubuntu' not in str(operating_system.get('name','')).lower() or not str(operating_system.get('version','')).startswith('24.04'):
        raise ValueError('Managed Timeweb profile requires Ubuntu 24.04 AMD64')
    key=ensure_key(state)
    value={'format':1,'provider':'timeweb','config':target(config),'ssh_key_sha256':sha(key.with_suffix('.pub')),
        'presets':prices,'changes':['dedicated project (unless selected)','SSH key','Linux VM and public IPv4',
        'private S3 bucket','stateless HTTPS App Platform gateway'],
        'costs':'Review current catalog prices and metered IPv4, storage, traffic and requests. No account spending cap is configured.',
        'credential_scope':'Timeweb bucket API may return account-wide S3 credentials. These are kept privately and sent only to the owner VM. Cloud API token is stored on the VM for gateway renewal.',
        'preservation':'No automatic deletion on failure. Retained resources remain billable.',
        'pricing_url':'https://timeweb.cloud/prices','dns_wait_seconds':config.get('dns_timeout',1800)}
    value['plan_sha256']=digest(value)
    if persist:save(state/'timeweb-plan.json',value)
    return value


def approved(state, approval=None):
    transition=state/'timeweb-vm-selection.json'
    if transition.exists() and not read(transition).get('complete'):
        raise ValueError('VM selection interrupted. Resume timeweb-select-vm with its approved hash before applying.')
    review=read(state/'timeweb-plan.json');expected=review['plan_sha256']
    if digest({k:v for k,v in review.items() if k!='plan_sha256'})!=expected:raise ValueError('Timeweb plan changed')
    if target(read(state/'deployment.json'))!=review['config'] or sha((state/'id_ed25519').with_suffix('.pub'))!=review['ssh_key_sha256']:
        raise ValueError('Deployment or SSH key changed. Review a new plan before proceeding')
    old=read(state/'timeweb-approved.json') if (state/'timeweb-approved.json').exists() else {}
    if approval!=expected and old.get('plan_sha256')!=expected:raise ValueError('Review Timeweb costs, then pass --approve-plan '+expected)
    save(state/'timeweb-approved.json',{'plan_sha256':expected})
    return review['config']


class Journal:
    def __init__(self,state,config):
        self.state=state;self.path=state/'timeweb-journal.json'
        self.value=read(self.path) if self.path.exists() else {'config':config,'resources':{},'stage':'not_started'}
        if self.value['config']!=config:raise ValueError('Timeweb state belongs to another installation')
    def save(self):save(self.path,self.value)
    def operation(self,stage,fn):
        self.value['stage']=stage;self.save()
        print('Timeweb stage: '+stage,file=sys.stderr,flush=True)
        try:result=fn()
        except Exception as error:
            self.value['last_error']={'stage':stage,'type':type(error).__name__,'http_status':error.status if isinstance(error,TimewebError) else None,
                'provider_error_code':error.error_code if isinstance(error,TimewebError) else None}
            self.save();raise
        self.value.pop('last_error',None);self.save();return result
    def resource(self,api,name,path,key,body,verify):
        def unpack(response,required=False):
            # Timeweb's SSH-key docs name `ssh-key`; the live API returns `ssh_key`.
            for candidate in ((key,'ssh_key') if key=='ssh-key' else (key,)):
                if candidate in response:return response[candidate]
            if required:raise ValueError('Missing resource response envelope: '+name+'. Reconcile the recorded creation before retrying.')
            return response
        item=self.value['resources'].setdefault(name,{'path':path,'key':key,'body_sha256':digest(body)})
        if item['body_sha256']!=digest(body):raise ValueError('Resource request changed: '+name)
        if item.get('id') is None:
            if item.get('submitted'):raise ValueError('Creation outcome unknown for '+name+'. Inspect the cloud and run timeweb-reconcile with its reviewed ID. No duplicate created.')
            item['submitted']=True;self.save()
            try:
                value=unpack(api.request(path,method='POST',body=body),required=True)
            except TimewebError as error:
                if error.status in (400,401,403,422,429) or (error.status==409 and error.error_code=='no_free_node'):
                    item['submitted']=False;self.save()
                raise
            item['id']=value.get('id',value.get('provider_id'));self.save()
            if item['id'] is None:raise ValueError('Creation returned no resource ID. Reconcile before continuing')
        value=api.request(path+'/'+str(item['id']))
        value=unpack(value)
        returned=value.get('id',value.get('provider_id'))
        if returned is not None and str(returned)!=str(item['id']):raise ValueError('Returned resource ID differs: '+name)
        if not verify(value):raise ValueError('Resource identity/configuration mismatch: '+name)
        return value


def status(state):
    p=state/'timeweb-journal.json';j=read(p) if p.exists() else {}
    resources=j.get('resources',{})
    return {'provider':'timeweb','stage':j.get('stage','not_started'),
        'resources':{k:{'id':v.get('id'),'creation_unresolved':bool(v.get('submitted') and v.get('id') is None)} for k,v in resources.items()},
        'last_error':j.get('last_error'),'dns':read(state/'dns-readiness.json') if (state/'dns-readiness.json').exists() else None,
        'next':'Resume the SAME --state. No resources are automatically deleted. For an unknown creation outcome, review its ID and use timeweb-reconcile.'}


def reconcile(state,stage,ident):
    # Save the explicitly supplied ID only after the normal request verifier runs
    # on the next resume; nothing is mutated or adopted by name here.
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        path=state/'timeweb-journal.json';j=read(path);item=j['resources'][stage]
        if item.get('id') is not None:raise ValueError('Resource already recorded')
        if not item.get('submitted'):raise ValueError('No ambiguous creation to reconcile')
        if not re.fullmatch(r'[a-zA-Z0-9-]+',str(ident)):raise ValueError('Invalid resource ID')
        item['id']=int(ident) if str(ident).isdigit() else ident;save(path,j)
    return {'recorded':stage,'next':'Resume installation to verify this resource before using it'}


def retry_deploy(state,approval=None):
    """Explicitly review a failed/ambiguous publication before allowing another POST."""
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        j=read(state/'timeweb-journal.json');ident=j['resources']['app']['id']
        path=state/'timeweb-publish.json';api=API(state)
        app=api.request('/api/v1/apps/'+str(ident))['app']
        if app.get('name')!=j['config']['name']+'-edge':raise ValueError('Wrong app')
        if app.get('commit_sha')!=j['config']['source_commit']:raise ValueError('App source changed')
        if path.exists():
            attempt=read(path)
        else:
            # The very first build may fail before DNS or TLS configuration exists.
            # Retry the same placeholder app, with its own durable publication intent.
            if app.get('envs'):raise ValueError('Unexpected configuration on initial app')
            path=state/'timeweb-bootstrap-publish.json'
            attempt={'desired':digest({'env':{},'commit':j['config']['source_commit']})}
        rows=api.request('/api/v1/apps/'+str(ident)+'/deploys').get('deploys',[])
        if any(v.get('status') not in ('success','failed','failure','error','cancelled','canceled','stopped') for v in rows):
            raise ValueError('A deployment is still pending. Wait and resume; no repeated POST allowed')
        preview={'app_id':ident,'desired':attempt['desired'],'deploys':[{k:v.get(k) for k in ('id','status','commit_sha')} for v in rows],
            'effect':'Allow one new deployment of the same app/configuration on the next install --https. No resource creation or deletion.'}
        preview['plan_sha256']=digest(preview)
        if approval is None:return preview
        if approval!=preview['plan_sha256']:raise ValueError('Deployment retry preview changed')
        save(path,{'desired':attempt['desired'],'baseline_deploys':[v['id'] for v in rows],'submitted':False})
        return {'retry_authorized':True,'app_id':ident,'next':'Resume install --https with same state'}


def cloud_init(config,key):
    cidr=str(ipaddress.ip_network(config['ssh_cidr'],strict=True))
    return '#cloud-config\n'+json.dumps({'users':[{'name':'pcadmin','groups':'sudo','shell':'/bin/bash',
        'sudo':['ALL=(ALL) NOPASSWD:ALL'],'lock_passwd':True,'ssh_authorized_keys':[key]}],
        'ssh_pwauth':False,'disable_root':True,'package_update':True,
        'packages':['python3','python3-venv','python3-pip','cron','ufw'],
        'runcmd':[['ufw','default','deny','incoming'],['ufw','default','allow','outgoing'],
            ['ufw','allow','from',cidr,'to','any','port','22','proto','tcp'],
            ['ufw','allow','9443/tcp'],['ufw','--force','enable']]})


def server_ip(value):
    addresses=[]
    for network in value.get('networks',[]):
        for row in network.get('ips',[]):
            address=row.get('ip') if isinstance(row,dict) else row
            try:
                ip=ipaddress.ip_address(address)
                if ip.version==4 and ip.is_global:addresses.append(str(ip))
            except ValueError:pass
    if len(set(addresses))!=1:raise ValueError('Server has not returned one public IPv4 address')
    return addresses[0]


def wait_app(api,ident,name,timeout=1200):
    deadline=time.monotonic()+timeout
    while True:
        value=api.request('/api/v1/apps/'+str(ident))['app']
        if value.get('name')!=name:raise ValueError('App identity mismatch')
        if value.get('status')=='active':return value
        if value.get('status') in ('failed','failure','error','deploy_fail'):raise ValueError('Timeweb deployment failed. App retained. Inspect its deploy logs and resume the same state.')
        if time.monotonic()>=deadline:raise ValueError('Timeweb app still deploying. Resume same state later.')
        time.sleep(10)


def apply(state,approval=None):
    config=approved(state,approval)
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        return provision(state,config)


def verify_server(value,config,public_key):
    if str(value.get('cloud_init','')).strip()=='hidden-by-api-key-policy':
        raise ValueError('Timeweb token masks secrets. Disable "Hide passwords and sensitive data" for the installer token, then resume the same state.')
    return (value.get('name')==config['name'] and value.get('cloud_init')==cloud_init(config,public_key)
        and value.get('preset_id')==config['server_preset_id'] and value.get('availability_zone')==config['availability_zone'])


def ensure_server_ipv4(api,j,vm):
    """One explicit IPv4 purchase; a lost response is reconciled from the same VM."""
    try:return server_ip(vm)
    except ValueError:pass
    # Never buy an additional address if a public IPv4 exists but is ambiguous.
    if any(row.get('type')=='ipv4' for network in vm.get('networks',[]) for row in network.get('ips',[]) if isinstance(row,dict)):
        raise ValueError('Review existing VM IPv4 addresses before continuing')
    item=j.value.setdefault('server_ipv4',{'server_id':vm['id']})
    if item['server_id']!=vm['id']:raise ValueError('IPv4 target changed')
    if item.get('submitted'):
        raise ValueError('IPv4 creation outcome pending or unknown. Inspect the same VM and resume after its address appears; no duplicate IP purchased.')
    item['submitted']=True;j.save()
    # No automatic retry after any error: reconcile through the VM network list.
    reply=api.request('/api/v1/servers/'+str(vm['id'])+'/ips',method='POST',body={'type':'ipv4'})
    address=(reply.get('server_ip') or {}).get('ip')
    if address:item['ip']=address;j.save()
    current=api.request('/api/v1/servers/'+str(vm['id']))['server']
    try:return server_ip(current)
    except ValueError:raise ValueError('IPv4 requested; assignment pending. Resume the same state, no second address will be requested.') from None


def provision(state,config):
    api=API(state);j=Journal(state,config);name=config['name']
    def infrastructure():
        project=config.get('project_id')
        if project:
            if api.request('/api/v1/projects/'+str(integer(project)))['project']['id']!=project:raise ValueError('Wrong project')
        else:
            project=j.resource(api,'project','/api/v1/projects','project',{'name':name,'description':'Portable Context'},lambda v:v.get('name')==name)['id']
        public_key=(state/'id_ed25519.pub').read_text().strip()
        key=j.resource(api,'ssh_key','/api/v1/ssh-keys','ssh-key',{'name':name,'body':public_key,'is_default':False},lambda v:v.get('name')==name and v.get('body','').strip()==public_key)
        vm=j.resource(api,'server','/api/v1/servers','server',{'name':name,'project_id':project,
            'preset_id':config['server_preset_id'],'os_id':config['os_id'],'ssh_keys_ids':[key['id']],
            'availability_zone':config['availability_zone'],
            'cloud_init':cloud_init(config,public_key)},lambda v:verify_server(v,config,public_key))
        host=ensure_server_ipv4(api,j,vm)
        bucket=j.resource(api,'bucket','/api/v1/storages/buckets','bucket',{'name':config['bucket'],'type':'private',
            'preset_id':config['storage_preset_id'],'project_id':project,'description':'Portable Context backups'},
            lambda v:v.get('name')==config['bucket'] and v.get('project_id')==project and v.get('type')=='private')
        endpoint=bucket['hostname']
        if any(str(bucket.get(k,'')).strip() in ('','hidden-by-api-key-policy') for k in ('access_key','secret_key')):
            raise ValueError('S3 credentials missing or hidden by token policy. Disable secret masking and resume; bucket retained.')
        if not endpoint.startswith('https://'):endpoint='https://'+endpoint
        from urllib.parse import urlsplit
        parsed=urlsplit(endpoint)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.path not in ('','/') or not (parsed.hostname or '').endswith('.twcstorage.ru'):
            raise ValueError('Review unexpected S3 endpoint before sending backup credentials')
        save(state/'backup-config.json',{'bucket':bucket['name'],'endpoint':endpoint.rstrip('/'),'region':'ru-1',
            'access_key':bucket['access_key'],'secret_key':bucket['secret_key']})
        current=read(state/'deployment.json')
        if current.get('host') and current['host']!=host:raise ValueError('VM IP changed. Explicit migration required')
        current['host']=host;save(state/'deployment.json',current)
        return {'host':host,'project_id':project,'server_id':vm['id'],'bucket_id':bucket['id'],'next':'install --https with the same state'}
    return j.operation('infrastructure',infrastructure)


def edge_env(host,ip,generation,revision='initial'):
    from gateway import edge_config
    return {k:base64.b64encode(v).decode() for k,v in {
        'PC_EDGE_CONFIG_B64':edge_config(host,ip,revision=revision).encode(),
        'PC_EDGE_CA_B64':(generation/'origin-ca.pem').read_bytes(),
        'PC_EDGE_CERT_B64':(generation/'edge.pem').read_bytes(),
        'PC_EDGE_KEY_B64':(generation/'edge-key.pem').read_bytes()}.items()}


def publish(api,app,env,commit,path):
    """Replay-safe configuration followed by journaled deployment of the same app."""
    wanted=digest({'env':env,'commit':commit});old=read(path) if path.exists() else None
    if old and old['desired']!=wanted:raise ValueError('Another edge publication is pending')
    attempt=old or {'desired':wanted,'baseline_deploys':[v['id'] for v in api.request('/api/v1/apps/'+str(app['id'])+'/deploys').get('deploys',[])]}
    save(path,attempt)
    if app.get('envs')!=env:
        if old and old.get('submitted'):raise ValueError('Edge environment changed during deployment. Inspect same app before retrying')
        api.request('/api/v1/apps/'+str(app['id']),method='PATCH',body={'envs':env,'is_auto_deploy':False})
    if attempt.get('submitted'):
        deploys=api.request('/api/v1/apps/'+str(app['id'])+'/deploys').get('deploys',[])
        fresh=[v for v in deploys if v['id'] not in attempt['baseline_deploys'] and v.get('commit_sha')==commit]
        if not fresh:return False
        if any(v.get('status') in ('failed','failure','error') for v in fresh):raise ValueError('Edge deploy failed. Preserve app and inspect logs.')
        return any(v.get('status')=='success' for v in fresh)
    attempt['submitted']=True;save(path,attempt)
    try:api.request('/api/v1/apps/'+str(app['id'])+'/deploy',method='POST',body={'commit_sha':commit})
    except TimewebError as error:
        if error.status in (400,401,403,422,429):attempt['submitted']=False;save(path,attempt)
        raise
    return False


def install(state,approval=None):
    config=approved(state,approval)
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        return install_locked(state,config)


def install_locked(state,config):
    from setup import install as memory_install, resume_memory, recovery_kit, ssh_read, ssh_args
    from https_setup import remote, check_public, save_client, owner_login
    from gateway import issue_credentials
    from dns_ready import wait as dns_wait
    api=API(state);name=config['name']+'-edge'
    provision(state,config)
    j=Journal(state,config)
    memory=j.operation('memory',lambda:resume_memory(state) or memory_install(state))
    def architecture():
        result=ssh_read(ssh_args(state,read(state/'deployment.json')),'uname -m',label='Timeweb server architecture')
        if result.returncode or result.stdout.strip() not in ('x86_64','amd64'):
            raise ValueError('Managed Timeweb HTTPS requires a Linux AMD64 VM. No edge app created.')
        return {'architecture':'amd64'}
    j.operation('architecture',architecture)
    def create_edge():
        class GitAPI:
            def request(self,path,**kwargs):
                result=api.request(path,**kwargs)
                if 'repositories' in result:result['provider']={**result.get('provider',{}),'repositories':result['repositories']}
                return result
        provider=j.resource(GitAPI(),'git','/api/v1/vcs-provider','provider',{'provider_type':'git','url':config['source_url']},
            lambda v:bool(v.get('repositories')))
        repos=provider.get('repositories',[])
        repo=next((x for x in repos if x.get('url','').rstrip('/').removesuffix('.git')==config['source_url'].rstrip('/').removesuffix('.git')),None)
        if not repo:raise ValueError('Public source repository not available')
        body={'name':name,'comment':'Portable Context HTTPS gateway','type':'backend','language':'docker','framework':'docker',
            'provider_id':j.value['resources']['git']['id'],'repository_id':repo['id'],
            'branch_name':config['source_branch'],'commit_sha':config['source_commit'],'build_cmd':'','run_cmd':'',
            'is_auto_deploy':False,'preset_id':config['app_preset_id'],'build_entry_path':'/deploy/timeweb-edge','health_check':'/_pc/health','envs':{}}
        # POST is v2, GET is v1 on the established App Platform API.
        class AppAPI:
            def request(self,path,**kwargs):
                if kwargs.get('method','GET')=='GET':path=path.replace('/api/v2/apps/','/api/v1/apps/')
                return api.request(path,**kwargs)
        return j.resource(AppAPI(),'app','/api/v2/apps','app',body,
            lambda v:v.get('name')==name and v.get('repository_id') in (None,repo['id']) and v.get('commit_sha')==config['source_commit'])
    app=j.operation('edge',create_edge)
    bootstrap_path=state/'timeweb-bootstrap-publish.json'
    if bootstrap_path.exists():
        def retry_initial_build():
            deadline=time.monotonic()+1200
            while True:
                current=api.request('/api/v1/apps/'+str(app['id']))['app']
                if publish(api,current,{},config['source_commit'],bootstrap_path):return {'published':True}
                if time.monotonic()>=deadline:raise ValueError('Initial edge deployment pending. Resume same state.')
                time.sleep(10)
        # A completed initial retry must not clear the later live TLS environment.
        if not (state/'timeweb-publish.json').exists():
            j.operation('edge-retry',retry_initial_build)
    app=j.operation('edge-deploy',lambda:wait_app(api,app['id'],name))
    public=origin(app);host=public.removeprefix('https://')
    folder=private_directory(state/'https');generation=folder/'generation'
    j.operation('dns',lambda:dns_wait(state,public,app['ip'],timeout=config.get('dns_timeout',1800)))
    if not generation.exists():
        with tempfile.TemporaryDirectory(prefix='seed-',dir=folder) as tmp:
            candidate=Path(tmp)/'generation';issue_credentials(candidate,host);candidate.rename(generation)
    # After initial installation the rotation controller owns active certificates.
    if (state/'rotation-installed.json').exists():
        local=j.operation('server-check',lambda:remote(state,'check',public))
    else:
        local=j.operation('oauth',lambda:remote(state,'configure',public,generation))
        env=edge_env(host,read(state/'deployment.json')['host'],generation)
        def publish_edge():
            deadline=time.monotonic()+1200
            while True:
                current=api.request('/api/v1/apps/'+str(app['id']))['app']
                if publish(api,current,env,config['source_commit'],state/'timeweb-publish.json'):return {'published':True}
                if time.monotonic()>=deadline:raise ValueError('Edge deployment pending. Resume same state.')
                time.sleep(10)
        j.operation('publish',publish_edge)
        from timeweb_rotation import install as rotation_install
        options={'backend':'timeweb','public_host':host,'origin_ip':read(state/'deployment.json')['host'],
            'app_id':app['id'],'app_name':name,'source_commit':config['source_commit'],'origin_port':9443,
            'application_port':8767,'identity_port':14120,'origin_service':local['origin_service']}
        j.operation('renewal',lambda:rotation_install(state,options,generation))
    checks=j.operation('public-check',lambda:check_public(public,600))
    kit=j.operation('recovery',lambda:recovery_kit(state))
    receipt={'installed':True,'provider':'timeweb','origin':public,'mcp_url':public+'/mcp','workspace_id':memory['workspace_id'],
        'client_id':'portable-context-desktop','callback_url':'http://127.0.0.1:14881/callback',
        'scopes':['openid','offline_access','memory:read','memory:write'],'checks':checks,'recovery_kit':kit,
        'rotation':read(state/'rotation-installed.json')['schedule']}
    save(state/'https-installed.json',receipt);save_client(state,receipt)
    if not (state/'owner-enrollment.json').exists():save(state/'owner-enrollment.json',owner_login(state))
    j.value['stage']='complete';j.save()
    return {**receipt,'owner_login':str(state/'owner-enrollment.json'),'client_config':str(state/'client-https.json')}
