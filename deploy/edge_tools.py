"""Download locked upstream gateway tools without Docker or a system install."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile


def install(tool, directory):
    system = {'Windows': 'windows', 'Linux': 'linux', 'Darwin': 'darwin'}[platform.system()]
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    lock = json.loads((Path(__file__).parent / 'edge/tool-lock.json').read_text())[tool]
    target = system + '_' + str(arch)
    if target not in lock['platforms']:
        raise ValueError('No pinned gateway tool for ' + target)
    entry = lock['platforms'][target]
    directory = Path(directory).resolve() / (tool + '-' + lock['version']) / target
    directory.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(entry['url'], headers={'User-Agent': 'portable-context-installer'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('Gateway tool checksum mismatch')
    name = tool + ('.exe' if system == 'windows' else '')
    if entry['url'].endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError('Gateway binary is not a regular archive member')
            binary = archive.extractfile(member).read()
    destination = directory / name
    pending = destination.with_suffix('.partial')
    pending.write_bytes(binary)
    pending.chmod(0o700)
    pending.replace(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', choices=['caddy', 'crane'])
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--ci', action='store_true')
    args = parser.parse_args()
    binary = install(args.tool, args.directory)
    if args.ci:
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
            stream.write('PC_TEST_' + args.tool.upper() + '=' + str(binary) + '\n')
    print(binary)
