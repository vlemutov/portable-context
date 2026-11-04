"""Pinned restic installation. Only official artifacts with embedded SHA256 are accepted."""
import bz2
import hashlib
import io
from pathlib import Path
import platform
import urllib.request
import zipfile

VERSION='0.19.1'
HASHES={
 'linux_amd64':'f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c',
 'linux_arm64':'a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465',
 'darwin_amd64':'c38d579622cf602f665234c5a8c315030b6cf70656028fe6dc29a786b60e5f35',
 'darwin_arm64':'7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143',
 'windows_amd64':'da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d',
}


def install(directory):
    system=platform.system().lower()
    machine=platform.machine().lower()
    arch='arm64' if machine in ('arm64','aarch64') else 'amd64' if machine in ('x86_64','amd64') else machine
    # Official restic has no Windows ARM binary; Windows 11 supports the x64 build via emulation.
    if system=='windows' and arch=='arm64': arch='amd64'
    target=system+'_'+arch
    if target not in HASHES: raise ValueError('Unsupported restic platform: '+target)
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    suffix='.zip' if system=='windows' else '.bz2'
    name='restic_'+VERSION+'_'+target+suffix
    package=directory/name
    if not package.exists() or hashlib.sha256(package.read_bytes()).hexdigest()!=HASHES[target]:
        with urllib.request.urlopen('https://github.com/restic/restic/releases/download/v'+VERSION+'/'+name,timeout=120) as r:
            data=r.read()
        if hashlib.sha256(data).hexdigest()!=HASHES[target]: raise ValueError('Restic artifact checksum mismatch')
        package.write_bytes(data)
    data=package.read_bytes()
    executable=directory/('restic-'+VERSION+('.exe' if system=='windows' else ''))
    if suffix=='.zip':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=[n for n in z.namelist() if n.endswith('.exe')]
            if len(names)!=1: raise ValueError('Unexpected restic archive')
            binary=z.read(names[0])
    else: binary=bz2.decompress(data)
    if not executable.exists() or executable.read_bytes()!=binary: executable.write_bytes(binary)
    executable.chmod(0o700)
    return str(executable.resolve())


if __name__=='__main__':
    import argparse,os
    p=argparse.ArgumentParser(); p.add_argument('directory',nargs='?'); p.add_argument('--ci',action='store_true')
    args=p.parse_args()
    if args.ci:
        binary=install(Path(os.environ['RUNNER_TEMP'])/'pc-restic')
        with open(os.environ['GITHUB_ENV'],'a',encoding='utf-8') as stream: stream.write('PC_TEST_RESTIC='+binary+'\n')
    elif args.directory: binary=install(args.directory)
    else: p.error('directory or --ci required')
    print(binary)
