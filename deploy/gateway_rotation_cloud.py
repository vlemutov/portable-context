"""Cloud.ru publication and local Linux origin adapter for journaled rotation."""
import base64
from datetime import datetime, timedelta
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

from cloud_gateway import CloudAPI, CloudAPIError, NoRedirect, container_request, public_origin
from gateway import edge_config, origin_config, hostname, port
from gateway_rotation import read, write, utc, materialize, durable_text
from portable_context.core import atomic_write


def tls_probe(address, listen, host, server, client, expected=None):
    """New TLS connection, exact server leaf and optional transport revision."""
    context=ssl.create_default_context(cafile=str(server/'origin-ca.pem'))
    context.load_cert_chain(client/'edge.pem',client/'edge-key.pem')
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    expected_der=x509.load_pem_x509_certificate((server/'origin.pem').read_bytes()).public_bytes(serialization.Encoding.DER)
    with socket.create_connection((address,listen),timeout=5) as sock:
        with context.wrap_socket(sock,server_hostname=host) as conn:
            if conn.getpeercert(binary_form=True)!=expected_der: raise ValueError('Origin still serves another generation')
            conn.sendall(('GET /_pc/transport HTTP/1.1\r\nHost: '+host+'\r\nConnection: close\r\n\r\n').encode())
            response=http.client.HTTPResponse(conn);response.begin();body=response.read(10240).decode()
            if response.status!=200 or (expected is not None and body!=expected):
                raise ValueError('Origin revision not ready')
            return body


class LocalOrigin:
    def __init__(self,state,config):
        self.state=Path(state);self.config=config

    def origin(self,certificate,trusts,revision,client):
        config=self.config; bundle=materialize(self.state,revision,certificate,trusts,'origin')
        content=origin_config(config['public_host'],bundle,config.get('origin_port',9443),
            config.get('application_port',8767),config.get('identity_port',14120),revision=revision)
        candidate=bundle/('origin-'+hashlib.sha256(content.encode()).hexdigest()[:16]+'.Caddyfile')
        durable_text(candidate,content)
        binary=Path(config['caddy']).resolve();live=Path(config['origin_config']).resolve()
        unit=config.get('origin_service','pc-gateway-origin.service')
        import re
        if not re.fullmatch(r'pc-[a-zA-Z0-9_-]+\.service',unit):raise ValueError('Invalid origin service')
        subprocess.run([str(binary),'validate','--config',str(candidate),'--adapter','caddyfile'],check=True,capture_output=True)
        pid=int(subprocess.run(['systemctl','--user','show',unit,'--property=MainPID','--value'],check=True,capture_output=True,text=True).stdout.strip())
        if pid<=1:raise ValueError('Origin service is not running')
        proc=Path('/proc')/str(pid)
        if proc.stat().st_uid!=os.getuid():raise ValueError('Origin belongs to another user')
        argv=(proc/'cmdline').read_bytes().decode().split('\0')
        if (str(binary) not in argv or '--resume' in argv or '--config' not in argv
                or Path(argv[argv.index('--config')+1]).resolve()!=live):
            raise ValueError('Origin service does not match selected runtime')
        baseline=self.state/'pre-rotation-origin.Caddyfile'
        known=[baseline]+list((self.state/'bundles').glob('*/origin*.Caddyfile'))
        if not any(p.is_file() and p.read_bytes()==live.read_bytes() for p in known):
            raise ValueError('Origin configuration changed outside the rotation controller')
        # Complete config points only at an immutable key set. SIGUSR1 retains
        # listeners and in-flight requests; no public administrative API is opened.
        durable_text(live,content)
        os.kill(pid,signal.SIGUSR1)
        for _ in range(30):
            try:
                tls_probe('127.0.0.1',config.get('origin_port',9443),config['public_host'],certificate,client,revision)
                return True
            except (OSError,ValueError):time.sleep(.1)
        return False


def desired_signature(value):
    """Ignore provider response-only fields; compare the deployed executable inputs."""
    template=value['template'];container=template['containers'][0]
    selected={k:container.get(k) for k in ('image','command','args','containerPort')}
    selected['env']=sorted((e['name'],e.get('type') or '',e.get('value','')) for e in container['env'])
    return hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()


class CloudBackend(LocalOrigin):
    def __init__(self,state):
        config=read(Path(state)/'config.json')
        if config.get('backend')!='cloudru':raise ValueError('Expected Cloud.ru rotation configuration')
        hostname(config['public_host']);ipaddress.ip_address(config['origin_ip'])
        super().__init__(state,config)
        self.api=CloudAPI(config['cloud_state'],config['project_id'])
        self.opener=urllib.request.build_opener(NoRedirect())

    def container(self):
        value=self.api.request('containers','/v2/containers/'+self.config['container_name'])
        if (value.get('id')!=self.config['container_id'] or value.get('projectId')!=self.config['project_id']
                or public_origin(value)!='https://'+self.config['public_host']):
            raise ValueError('Cloud container identity or public address changed')
        if value['template']['containers'][0]['image']!=self.config['image']:
            raise ValueError('Edge image changed; coordinate the updater with rotation settings')
        return value

    def ensure_secret(self,certificate,journal):
        name=self.config['container_name']+'-'+journal['new']
        expected=base64.b64encode((certificate/'edge-key.pem').read_bytes())
        try:
            value=self.api.request('secrets','/v2/version/'+name)
        except CloudAPIError as error:
            if error.status!=404:raise
            # Deterministic name: if the POST reply is lost, the next GET proves
            # the existing payload. Never overwrite a colliding secret or create a new name.
            self.api.request('secrets','/v2/secret',method='POST',body={
                'projectId':self.config['project_id'],'path':name,
                'description':'Portable Context mTLS '+journal['new'],
                'payload':base64.b64encode(expected).decode()})
            value=self.api.request('secrets','/v2/version/'+name)
        if base64.b64decode(value['payload'],validate=True)!=expected:
            raise ValueError('Existing rotation secret has another payload')
        return name

    def public_probe(self,edge_revision,origin_revision=None):
        url='https://'+self.config['public_host']+'/_pc/transport'
        req=urllib.request.Request(url,headers={'Cache-Control':'no-cache'})
        with self.opener.open(req,timeout=30) as response:
            body=response.read(1024).decode()
            return (response.status==200 and response.headers.get('X-PC-Edge-Revision')==edge_revision
                    and (origin_revision is None or body==origin_revision))

    def edge(self,certificate,trusts,revision,journal):
        current=self.container()
        secret=self.ensure_secret(certificate,journal)
        bundle=materialize(self.state,revision,certificate,trusts,'edge')
        content=edge_config(self.config['public_host'],self.config['origin_ip'],
            origin_port=self.config.get('origin_port',9443),revision=revision).encode()
        body=container_request(self.config['project_id'],self.config['container_name'],self.config['image'],
            config=content,certificate=(bundle/'edge.pem').read_bytes(),ca=(bundle/'origin-ca.pem').read_bytes(),key_secret=secret)
        body.pop('metadata',None);body['configuration'].pop('privileged',None)
        wanted=desired_signature(body);actual=desired_signature(current)
        path=self.state/('publish-'+revision+'.json')
        attempt=read(path) if path.exists() else None
        if actual==wanted:
            # A configured template alone is not an active revision. Probe the
            # served edge stamp and the full mTLS path before advancing.
            if current.get('status')!='running':return False
            try:return self.public_probe(revision)
            except (OSError,ValueError):return False
        if current.get('status') not in ('running','active'):return False
        if attempt:
            # A crash can happen immediately before or after PATCH. Reconcile by
            # GET and a quiet period, then reapply only the SAME desired template.
            if actual!=attempt['previous']:raise ValueError('Unrelated container update detected')
            if utc()<datetime.fromisoformat(attempt['at'])+timedelta(minutes=5):return False
            if attempt['attempts']>=3:raise ValueError('Publication did not converge; keep overlap and diagnose')
        attempt={'previous':actual,'desired':wanted,'at':utc().isoformat(),
            'attempts':attempt['attempts']+1 if attempt else 1}
        write(path,attempt)
        result=self.api.request('containers','/v2/containers/'+self.config['container_name'],method='PATCH',body=body)
        write(path,{**attempt,'operation_id':result.get('id')})
        return False

    def verify(self,new,old,journal):
        origin_revision=journal['new']+'-origin_finalize'
        edge_revision=journal['new']+'-edge_finalize'
        if not self.public_probe(edge_revision,origin_revision):return False
        tls_probe('127.0.0.1',self.config.get('origin_port',9443),self.config['public_host'],new,new,origin_revision)
        try:
            tls_probe('127.0.0.1',self.config.get('origin_port',9443),self.config['public_host'],new,old)
        except (ssl.SSLError,ConnectionError,http.client.HTTPException):return True
        raise ValueError('Retired edge credential is still accepted')
