"""CI-only Docker smoke test with synthetic transport keys and no cloud account."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from gateway import edge_config, issue_credentials


def run(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs).stdout.strip()


def check_container(image,env,expected):
    ident=run(['docker','run','--rm','-d','-p','127.0.0.1::8080',*env,image])
    try:
        details=json.loads(run(['docker','inspect',ident]))[0]
        port=details['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        url='http://127.0.0.1:'+port
        deadline=time.monotonic()+30
        while True:
            try:
                with urllib.request.urlopen(url+'/_pc/health',timeout=2) as response:
                    assert response.read()==expected;break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.5)
        try:urllib.request.urlopen(url+'/mcp',timeout=3)
        except urllib.error.HTTPError as error:
            assert error.code in (404,503), error.code
        else:raise AssertionError('Unconfigured or wrong-host MCP must be denied')
    finally:
        subprocess.run(['docker','rm','-f',ident],check=False,capture_output=True)


def main():
    image='portable-context-timeweb-edge:ci'
    run(['docker','build','-t',image,str(Path(__file__).parent/'timeweb-edge')],timeout=300)
    check_container(image,[],b'Portable Context setup')
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp);keys=folder/'keys';issue_credentials(keys,'test.twc1.net')
        values={'PC_EDGE_CONFIG_B64':edge_config('test.twc1.net','192.0.2.1',revision='ci').encode(),
            'PC_EDGE_CERT_B64':(keys/'edge.pem').read_bytes(),'PC_EDGE_KEY_B64':(keys/'edge-key.pem').read_bytes(),
            'PC_EDGE_CA_B64':(keys/'origin-ca.pem').read_bytes()}
        env=folder/'env';env.write_text('\n'.join(k+'='+base64.b64encode(v).decode() for k,v in values.items())+'\n');env.chmod(0o600)
        check_container(image,['--env-file',str(env)],b'edge alive; not a memory readiness check')
    partial=subprocess.run(['docker','run','--rm','-e','PC_EDGE_CONFIG_B64=eA==',image],capture_output=True,timeout=15)
    assert partial.returncode!=0,'Partial TLS configuration must fail closed'
    print('Timeweb edge: placeholder, configured health, wrong-host denial and partial-config rejection passed')


if __name__=='__main__':main()
