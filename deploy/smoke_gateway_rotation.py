"""Real Caddy/mTLS, real systemd origin, killed controller at every stage.

Cloud publication is represented by a second local Caddy process here. A real
Cloud.ru revision publication is a separate pilot test, not claimed by this test.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from gateway import edge_config, origin_config, issue_credentials
from gateway_rotation import Rotation, initialize, materialize, read, write, utc, STAGES
from gateway_rotation_cloud import LocalOrigin, CloudBackend
from portable_context.core import atomic_write


def request(config,path='/_pc/transport',method='GET',body=None):
    with closing(http.client.HTTPConnection('127.0.0.1',config['edge_port'],timeout=15)) as conn:
        conn.request(method,path,body=body,headers={'Host':config['public_host']})
        r=conn.getresponse();return r.status,dict(r.getheaders()),r.read()


class LocalEdge(LocalOrigin):
    def public_probe(self,edge_revision,origin_revision=None):
        status,headers,body=request(self.config)
        return (status==200 and next((v for k,v in headers.items() if k.lower()=='x-pc-edge-revision'),None)==edge_revision
                and (origin_revision is None or body.decode()==origin_revision))

    def edge(self,certificate,trusts,revision,journal):
        bundle=materialize(self.state,revision,certificate,trusts,'edge')
        content=edge_config(self.config['public_host'],'127.0.0.1',bundle,
            self.config['edge_port'],self.config['origin_port'],revision=revision)
        path=Path(self.config['edge_config']);candidate=bundle/'edge.Caddyfile';atomic_write(candidate,content)
        subprocess.run([self.config['caddy'],'validate','--config',str(candidate),'--adapter','caddyfile'],check=True,capture_output=True)
        atomic_write(path,content);os.kill(self.config['edge_pid'],signal.SIGUSR1)
        for _ in range(40):
            try:
                if self.public_probe(revision):return True
            except (OSError,ValueError):pass
            time.sleep(.1)
        return False

    verify=CloudBackend.verify


def worker(state,clock,point):
    config=read(Path(state)/'config.json')
    controller=Rotation(state,LocalEdge(state,config),lambda:datetime.fromisoformat(clock))
    def kill(stage,where):
        if where==point:os._exit(77)  # Simulates death without finally/flush/cleanup.
    result=controller.tick(force=True,interrupt=kill if point else None)
    assert result['status'] in ('advanced','complete'),result
    print(json.dumps(result))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--caddy')
    parser.add_argument('--worker');parser.add_argument('--clock');parser.add_argument('--interrupt')
    args=parser.parse_args()
    if args.worker:return worker(args.worker,args.clock,args.interrupt)
    if sys.platform!='linux':raise ValueError('Systemd acceptance runs on Linux')
    caddy=str(Path(args.caddy).resolve())
    subprocess.run(['systemctl','--user','show-environment'],check=True,capture_output=True)
    unit='pc-rotation-test-'+uuid.uuid4().hex+'.service'
    unitpath=Path.home()/'.config/systemd/user'/unit;unitpath.parent.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();errors=[];counts={'requests':0,'posts':0};edge=None;app=None;monitor=None
    with tempfile.TemporaryDirectory(prefix='pc-rotation-test-') as tmp:
        root=Path(tmp);root.chmod(0o700)
        class App(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                self.send_response(200);self.end_headers()
                if self.path=='/events':
                    for i in range(250):
                        self.wfile.write(('data: '+str(i)+'\n\n').encode());self.wfile.flush();time.sleep(.04)
                else:self.wfile.write(b'alive')
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')));counts['posts']+=1
                if self.path=='/long-write':time.sleep(5)
                self.send_response(200);self.end_headers();self.wfile.write(body)
        app=ThreadingHTTPServer(('127.0.0.1',0),App)
        threading.Thread(target=app.serve_forever,daemon=True).start()
        def free():
            with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
        config={'public_host':'memory.example','caddy':caddy,'origin_port':free(),'edge_port':free(),
            'application_port':app.server_port,'identity_port':None,'origin_service':unit,
            'origin_config':str(root/'origin.Caddyfile'),'edge_config':str(root/'edge.Caddyfile')}
        issue_credentials(root/'seed','memory.example')
        state=root/'rotation';initialize(state,config,root/'seed');seed=state/'generations/initial'
        atomic_write(Path(config['origin_config']),origin_config(config['public_host'],seed,config['origin_port'],app.server_port,revision='initial'))
        atomic_write(Path(config['edge_config']),edge_config(config['public_host'],'127.0.0.1',seed,config['edge_port'],config['origin_port'],revision='initial'))
        try:
            unitpath.write_text('[Service]\nExecStart='+caddy+' run --config '+config['origin_config']+' --adapter caddyfile\nUMask=0077\nStandardOutput=null\nStandardError=null\n')
            subprocess.run(['systemctl','--user','daemon-reload'],check=True,capture_output=True)
            subprocess.run(['systemctl','--user','start',unit],check=True,capture_output=True)
            pid=subprocess.run(['systemctl','--user','show',unit,'--property=MainPID','--value'],check=True,capture_output=True,text=True).stdout.strip()
            edge=subprocess.Popen([caddy,'run','--config',config['edge_config'],'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            config['edge_pid']=edge.pid;write(state/'config.json',{**config,'format':'portable-context-rotation-1'})
            for _ in range(100):
                try:
                    if request(config)[2]==b'initial':break
                except OSError:pass
                time.sleep(.1)
            else:raise ValueError('Initial transport failed')
            def watch():
                while not stop.is_set():
                    try:
                        status,_,body=request(config,'/echo','POST',b'probe')
                        if status!=200 or body!=b'probe':errors.append('response')
                        counts['requests']+=1
                    except Exception as e:errors.append(type(e).__name__)
                    stop.wait(.03)
            monitor=threading.Thread(target=watch);monitor.start()
            special={}
            def long_request(name,path,method='GET',body=None):
                try:special[name]=request(config,path,method,body)
                except Exception as e:special[name]=type(e).__name__
            stream=threading.Thread(target=long_request,args=('stream','/events'));stream.start()
            mutation=threading.Thread(target=long_request,args=('mutation','/long-write','POST',b'committed-once'));mutation.start()
            clock=utc()
            for stage in STAGES:
                clock+=timedelta(minutes=16)
                for point in ('before','after',None):
                    command=[sys.executable,__file__,'--worker',str(state),'--clock',clock.isoformat()]
                    if point:command+=['--interrupt',point]
                    completed=subprocess.run(command,capture_output=True,text=True,timeout=90)
                    if completed.returncode!=(77 if point else 0):
                        raise ValueError('Controller stage failed: '+stage+' '+str(point)+' '+completed.stderr[-1200:])
                print(json.dumps({'recovered_stage':stage,'requests':counts['requests'],'failures':len(errors)}),flush=True)
            stream.join(timeout=20);mutation.join(timeout=20)
            stop.set();monitor.join(timeout=20)
            assert not errors,errors[:5]
            assert special['mutation'][0]==200 and special['mutation'][2]==b'committed-once'
            assert special['stream'][0]==200 and special['stream'][2].count(b'data: ')==250
            assert counts['posts']==counts['requests']+1,'Mutation was retried or lost'
            finalpid=subprocess.run(['systemctl','--user','show',unit,'--property=MainPID','--value'],check=True,capture_output=True,text=True).stdout.strip()
            assert finalpid==pid and edge.poll() is None
            assert read(state/'rotation.json')['complete']
            print(json.dumps({'rotation_verified':True,'killed_controllers':2*len(STAGES),'http_failures':0,
                'requests':counts['requests'],'uninterrupted_stream_events':250,'long_mutation_exactly_once':True,
                'origin_process_not_restarted':True,'old_certificate_rejected':True}),flush=True)
        finally:
            stop.set()
            if monitor:monitor.join(timeout=20)
            if edge:edge.terminate();edge.wait(timeout=15)
            subprocess.run(['systemctl','--user','stop',unit],capture_output=True)
            unitpath.unlink(missing_ok=True)
            subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)
            subprocess.run(['systemctl','--user','reset-failed',unit],capture_output=True)
            if app:app.shutdown();app.server_close()


if __name__=='__main__':main()
