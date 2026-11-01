"""Owner-operated installer: prepare -> catalog -> plan -> apply -> install -> check.

Run from a downloaded repository with Python 3.11+. No third-party dependencies for planning.
Cloud credentials and Terraform state live outside the repository in --state.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
import platform
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'deploy/cloudru'


def private_directory(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or path.is_relative_to(ROOT):
        raise ValueError('Deployment state must be outside the source repository')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        # Protect Terraform state (contains the generated backup access key) as well as SSH keys.
        identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
        sid = re.search(r'S-1-[0-9-]+', identity).group()
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F'],
                       check=True, capture_output=True)
    else:
        path.chmod(0o700)
    return path


def save(path, value):
    tmp = path.with_suffix(path.suffix + '.new')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def checked(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def bootstrap(state):
    """Install pinned tools into this deployment directory, never global PATH/config."""
    system = {'Windows': 'windows', 'Linux': 'linux', 'Darwin': 'darwin'}[platform.system()]
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
            'i386': '386', 'i686': '386', 'armv7l': 'arm'}.get(machine, machine)
    if system == 'windows' and arch == 'arm64':
        # Cloud.ru publishes no Windows ARM build. Windows 11's x64 emulation runs both tools.
        arch = 'amd64'
        print('Windows ARM: using the x64 Terraform/provider builds (requires x64 emulation).', file=sys.stderr)
    target = system + '_' + arch
    checksums = read(TEMPLATE / 'provider-checksums.json')
    if target not in checksums['sha256']:
        raise ValueError('Pinned Cloud.ru provider has no build for ' + target + '; use a supported desktop or AMD64 helper')
    directory = state / 'tools'
    directory.mkdir(exist_ok=True)

    def download(url, destination, expected=None):
        if destination.exists() and expected and sha(destination) == expected:
            return
        tmp = destination.with_suffix('.partial')
        request = urllib.request.Request(url, headers={'User-Agent': 'portable-context-setup'})
        print('Downloading ' + destination.name, file=sys.stderr, flush=True)
        try:
            with urllib.request.urlopen(request, timeout=60) as response, tmp.open('wb') as out:
                shutil.copyfileobj(response, out)
            if expected and sha(tmp) != expected:
                raise ValueError('Download checksum mismatch: ' + destination.name)
            tmp.replace(destination)
        finally:
            tmp.unlink(missing_ok=True)

    tf_version = '1.13.5'
    prefix = 'https://releases.hashicorp.com/terraform/' + tf_version + '/'
    sums = directory / 'terraform-SHA256SUMS'
    download(prefix + 'terraform_' + tf_version + '_SHA256SUMS', sums)
    filename = 'terraform_' + tf_version + '_' + target + '.zip'
    expected = next(line.split()[0] for line in sums.read_text().splitlines() if line.split()[-1] == filename)
    archive = directory / filename
    download(prefix + filename, archive, expected)
    binary = directory / ('terraform.exe' if system == 'windows' else 'terraform')
    with zipfile.ZipFile(archive) as zip_file:
        binary.write_bytes(zip_file.read(binary.name))
    binary.chmod(0o700)
    checked([binary, 'version'], stdout=subprocess.DEVNULL)
    mirror = directory / 'providers'
    provider_dir = mirror / 'registry.terraform.io/cloudru/cloud' / checksums['version'] / target
    provider_dir.mkdir(parents=True, exist_ok=True)
    provider = provider_dir / ('terraform-provider-cloud_v' + checksums['version'] + ('.exe' if system == 'windows' else ''))
    url = ('https://github.com/cloud-ru/evo-terraform/releases/download/v' + checksums['version']
           + '/terraform-provider-cloud_' + checksums['version'] + '_' + target)
    download(url, provider, checksums['sha256'][target])
    provider.chmod(0o700)
    (state / 'terraform.rc').write_text('provider_installation {\n filesystem_mirror {\n path = '
        + json.dumps(mirror.as_posix()) + '\n include = ["registry.terraform.io/cloudru/*"]\n }\n}\n')
    return {'terraform': str(binary), 'provider': checksums['version'], 'checksums': 'verified'}


def command_env(state, credentials=True):
    env = dict(os.environ)
    env['TF_IN_AUTOMATION'] = '1'
    env['TF_CLI_CONFIG_FILE'] = str(state / 'terraform.rc')
    # Debug logs may contain secrets or private plan values; never turn them on in the installer.
    env.pop('TF_LOG', None)
    env.pop('TF_LOG_PATH', None)
    if credentials:
        stored = read(state / 'credentials.json') if (state / 'credentials.json').exists() else {}
        for name in ('AUTH_KEY_ID', 'AUTH_SECRET'):
            if not env.get(name):
                env[name] = stored.get(name, '')
            if not env[name]:
                raise ValueError('Set AUTH_KEY_ID and AUTH_SECRET locally, or run the credentials command')
    return env


def terraform(state, args, *, credentials=True, folder=None, capture=False):
    local = state / 'tools' / ('terraform.exe' if os.name == 'nt' else 'terraform')
    binary = os.environ.get('PC_TERRAFORM') or (str(local) if local.exists() else shutil.which('terraform'))
    if not binary:
        raise ValueError('Terraform 1.9+ required; install it or set PC_TERRAFORM to its executable path')
    try:
        return checked([binary, '-chdir=' + str(folder or state / 'terraform'), *args],
                       env=command_env(state, credentials), capture_output=capture, text=True)
    except subprocess.CalledProcessError as exc:
        if capture:
            log = state / 'last-error.log'
            log.write_text((exc.stdout or '') + (exc.stderr or ''), encoding='utf-8')
            log.chmod(0o600)
            print('Private diagnostic log: ' + str(log), file=sys.stderr)
        raise


def prepare(state, mode):
    existing = state / 'deployment.json'
    if existing.exists():
        config = read(existing)
        if config['mode'] != mode:
            raise ValueError('This state directory belongs to another deployment mode')
        return config
    config = {'format': 1, 'mode': mode, 'name': 'pc-' + uuid.uuid4().hex[:12], 'ssh_user': 'pcadmin',
              'ssh_port': 22, 'schedule_backup': True}
    save(existing, config)
    if mode == 'cloudru':
        shutil.copyfile(TEMPLATE / 'terraform.rc', state / 'terraform.rc')
    return config


def validate_config(config):
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,40}', config['name']):
        raise ValueError('Invalid deployment name')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', config['ssh_user']):
        raise ValueError('Invalid SSH user')
    if not 1 <= int(config.get('ssh_port', 22)) <= 65535:
        raise ValueError('Invalid SSH port')
    host = config.get('host', '')
    if host and (host.startswith('-') or not re.fullmatch(r'[a-zA-Z0-9.:-]+', host)):
        raise ValueError('Host must be a hostname or IP, without shell syntax')
    if config['mode'] == 'cloudru':
        for field in ('project_id', 'tenant_id', 'image_id'):
            uuid.UUID(config[field])
        network = ipaddress.ip_network(config['ssh_cidr'], strict=True)
        if network.version != 4 or network.prefixlen < 8:
            raise ValueError('SSH access must use an explicit IPv4 CIDR, preferably your public IP/32')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', config['bucket']):
            raise ValueError('Invalid S3 bucket name')
        if int(config['disk_gb']) < 20:
            raise ValueError('Use at least 20 GB')


def ensure_key(state):
    key = state / 'id_ed25519'
    if not key.exists():
        if key.with_suffix('.pub').exists():
            raise ValueError('Public key exists but private key is missing; restore the deployment state')
        checked(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'portable-context', '-f', str(key)])
    return key


def render(state, config):
    validate_config(config)
    folder = state / 'terraform'
    folder.mkdir(exist_ok=True)
    shutil.copyfile(TEMPLATE / 'main.tf', folder / 'main.tf')
    key = ensure_key(state)
    values = {k: config[k] for k in ('project_id', 'tenant_id', 'name', 'zone', 'image_id',
                                    'flavor', 'ssh_cidr', 'bucket', 'disk_gb')}
    values['ssh_public_key'] = key.with_suffix('.pub').read_text().strip()
    save(folder / 'deployment.auto.tfvars.json', values)


def fingerprint(state):
    files = sorted(p for p in (state / 'terraform').iterdir()
                   if p.name.endswith(('.tf', '.tf.json', '.tfvars.json')) or p.name == '.terraform.lock.hcl')
    files += [state / 'deployment.json', state / 'terraform.rc']
    return hashlib.sha256(''.join(p.name + ':' + sha(p) for p in files).encode()).hexdigest()


def summarize_plan(data):
    resources = []
    for r in data.get('resource_changes', []):
        actions = r['change']['actions']
        if 'delete' in actions:
            raise ValueError('Plan deletes/replaces infrastructure. Export memory and review migration manually: ' + r['address'])
        if actions != ['no-op']:
            resources.append({'resource': r['address'], 'actions': actions})
    return resources


def plan(state):
    config = read(state / 'deployment.json')
    render(state, config)
    terraform(state, ['init', '-input=false', '-no-color'])
    terraform(state, ['validate', '-no-color'], credentials=False)
    terraform(state, ['plan', '-input=false', '-no-color', '-out=deployment.tfplan'], capture=True)
    data = json.loads(terraform(state, ['show', '-json', 'deployment.tfplan'], capture=True, credentials=False).stdout)
    resources = summarize_plan(data)
    report = {'plan_sha256': sha(state / 'terraform/deployment.tfplan'), 'configuration_sha256': fingerprint(state),
              'changes': resources, 'deployment': config['name'],
              'costs': 'Paid VM, SSD disk, public IPv4, S3 storage/requests/traffic. No live price quote is calculated.',
              'pricing_url': 'https://cloud.ru/calculator',
              'next': 'Review changes and current costs; apply only with --approve-plan <plan_sha256>.'}
    save(state / 'plan-review.json', report)
    return report


def apply(state, approved):
    review = read(state / 'plan-review.json')
    if (approved != review['plan_sha256'] or sha(state / 'terraform/deployment.tfplan') != approved
            or fingerprint(state) != review['configuration_sha256']):
        raise ValueError('Plan is missing, changed or not approved; run plan again')
    # Prevent double apply after an interrupted run: Terraform rejects stale state plans too.
    terraform(state, ['apply', '-input=false', '-no-color', 'deployment.tfplan'])
    outputs = json.loads(terraform(state, ['output', '-json'], capture=True).stdout)
    config = read(state / 'deployment.json')
    config['host'] = outputs['host']['value']
    save(state / 'deployment.json', config)
    save(state / 'backup-config.json', outputs['backup']['value'])
    (state / 'plan-review.json').unlink()
    return {'host': config['host'], 'next': 'install (resumable; no second VM is created)'}


def catalog(state, project_id):
    uuid.UUID(project_id)
    folder = state / 'catalog'
    folder.mkdir(exist_ok=True)
    main = (TEMPLATE / 'main.tf').read_text().split('# Credentials')[0]
    main += '\nprovider "cloudru" { project_id = ' + json.dumps(project_id) + ' }\n'
    for kind, plural in [('image', 'images'), ('flavor', 'flavors'), ('zone', 'zones')]:
        main += ('data "cloudru_evolution_compute_' + kind + '_collection" "list" { project_id = '
                 + json.dumps(project_id) + ' }\noutput "' + plural + '" { value = data.cloudru_evolution_compute_'
                 + kind + '_collection.list.' + plural + ' }\n')
    (folder / 'main.tf').write_text(main)
    terraform(state, ['init', '-input=false', '-no-color'], folder=folder)
    terraform(state, ['plan', '-input=false', '-no-color', '-out=catalog.tfplan'], folder=folder, capture=True)
    data = json.loads(terraform(state, ['show', '-json', 'catalog.tfplan'], folder=folder, capture=True).stdout)
    result = {k: v['value'] for k, v in data['planned_values']['outputs'].items()}
    save(state / 'catalog.json', result)
    return result


def bundle():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        paths = [ROOT / name for name in ('pyproject.toml', 'README.md', 'LICENSE')]
        paths += sorted((ROOT / 'src/portable_context').glob('*.py'))
        for path in paths:
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    data = buffer.getvalue()
    return {'bundle': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}


def ssh_args(state, config):
    validate_config(config)
    if not config.get('host'):
        raise ValueError('Server host is missing; apply the cloud plan or configure an existing server')
    key = Path(config.get('identity_file', state / 'id_ed25519')).expanduser().resolve()
    if not key.is_file():
        raise ValueError('SSH private key does not exist')
    # TOFU for a new deployment; changed host keys are rejected. No StrictHostKeyChecking=no.
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + str(state / 'known_hosts'), '-i', str(key),
            '-p', str(config.get('ssh_port', 22)), config['ssh_user'] + '@' + config['host']]


def install(state):
    config = read(state / 'deployment.json')
    ssh = ssh_args(state, config)
    if config['mode'] == 'cloudru':
        # Retry only readiness checks; never create new cloud resources on connection failure.
        for attempt in range(30):
            ready = subprocess.run(ssh + ['true'], capture_output=True)
            if ready.returncode == 0:
                break
            if attempt == 29:
                raise ValueError('SSH not ready. Check host, allowed CIDR and cloud-init; rerun install using the same state.')
            print('Waiting for SSH readiness...', file=sys.stderr, flush=True)
            time.sleep(10)
        checked(ssh + ['sudo cloud-init status --wait'], stdout=sys.stderr)
    payload = bundle()
    backup_file = state / 'backup-config.json'
    if config['mode'] == 'cloudru' and not backup_file.exists():
        # Recover outputs if apply completed before the local result was saved.
        outputs = json.loads(terraform(state, ['output', '-json'], capture=True).stdout)
        save(backup_file, outputs['backup']['value'])
    if backup_file.exists():
        payload['backup'] = read(backup_file)
    payload['schedule_backup'] = config.get('schedule_backup', True)
    script = base64.b64encode((ROOT / 'deploy/remote_install.py').read_bytes()).decode()
    remote = 'python3 -c ' + shlex.quote('import base64; exec(base64.b64decode(' + repr(script) + '))')
    completed = checked(ssh + [remote], input=json.dumps(payload), text=True, stdout=subprocess.PIPE)
    result = json.loads(completed.stdout)
    save(state / 'installed.json', result)
    command = shlex.quote(result['root'] + '/bin/mcp')
    args = ssh[1:] + [command]
    client = {'mcpServers': {'portable-context': {'command': 'ssh', 'args': args}}}
    save(state / 'client-config.json', client)
    return {'installed': True, 'workspace_id': result['memory']['workspace_id'], 'backup': result['backup'],
            'client_config': str(state / 'client-config.json'), 'next': 'check, then merge client configuration into your agent'}


async def check(state):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    entry = read(state / 'client-config.json')['mcpServers']['portable-context']
    with anyio.fail_after(60):
      async with stdio_client(StdioServerParameters(**entry)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool('memory_status', {})
            if result.is_error:
                raise ValueError('MCP memory_status failed')
            status = result.structured_content
            if status is None:
                status = json.loads(next(c.text for c in result.content if c.type == 'text'))
            status = status.get('result', status)
            expected = read(state / 'installed.json')['memory']['workspace_id']
            if status['workspace_id'] != expected:
                raise ValueError('Connected to an unexpected workspace')
            return {'mcp': 'verified', 'workspace_id': expected, 'messages': status['stats']['messages']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Private persistent directory OUTSIDE the source repo')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init')
    p.add_argument('--mode', choices=('cloudru', 'server'), required=True)
    sub.add_parser('credentials')
    sub.add_parser('bootstrap', help='Download verified Terraform and Cloud.ru provider into the private state directory')
    p = sub.add_parser('configure')
    p.add_argument('--file', type=Path, required=True, help='Non-secret JSON options; merged with persistent config')
    sub.add_parser('doctor')
    p = sub.add_parser('catalog')
    p.add_argument('--project-id', required=True)
    sub.add_parser('plan')
    p = sub.add_parser('apply')
    p.add_argument('--approve-plan', required=True, help='Exact SHA-256 of reviewed paid infrastructure plan')
    sub.add_parser('install')
    sub.add_parser('check')
    args = parser.parse_args(argv)
    os.umask(0o077)
    state = private_directory(args.state)
    if args.command == 'init':
        result = prepare(state, args.mode)
    elif args.command == 'bootstrap':
        result = bootstrap(state)
    elif args.command == 'credentials':
        import getpass
        if not sys.stdin.isatty():
            raise ValueError('Run credentials in your own interactive terminal; never paste keys into chat')
        values = {key: getpass.getpass(key + ': ') for key in ('AUTH_KEY_ID', 'AUTH_SECRET')}
        if not all(values.values()):
            raise ValueError('Both values are required')
        save(state / 'credentials.json', values)
        result = {'saved': True, 'note': 'Credentials stay on this computer; remove them after provisioning if desired.'}
    elif args.command == 'configure':
        config = read(state / 'deployment.json')
        values = read(args.file)
        allowed = {'project_id', 'tenant_id', 'zone', 'image_id', 'flavor', 'ssh_cidr', 'bucket', 'disk_gb',
                   'ssh_user', 'ssh_port', 'host', 'identity_file', 'schedule_backup'}
        if set(values) - allowed:
            raise ValueError('Unknown or secret configuration fields: ' + ', '.join(set(values) - allowed))
        config.update(values)
        config.setdefault('bucket', config['name'] + '-backups')
        config.setdefault('disk_gb', 40)
        validate_config(config)
        save(state / 'deployment.json', config)
        result = {'configured': True}
    elif args.command == 'doctor':
        local_tf = state / 'tools' / ('terraform.exe' if os.name == 'nt' else 'terraform')
        result = {'python': sys.version.split()[0], 'ssh': shutil.which('ssh'), 'ssh_keygen': shutil.which('ssh-keygen'),
                  'terraform': os.environ.get('PC_TERRAFORM') or (str(local_tf) if local_tf.exists() else shutil.which('terraform')),
                  'configured': (state / 'deployment.json').exists(),
                  'credentials_present': (state / 'credentials.json').exists() or bool(os.environ.get('AUTH_SECRET')),
                  'state': str(state)}
    elif args.command == 'catalog':
        result = catalog(state, args.project_id)
    elif args.command == 'plan':
        result = plan(state)
    elif args.command == 'apply':
        result = apply(state, args.approve_plan)
    elif args.command == 'install':
        result = install(state)
    else:
        import anyio
        result = anyio.run(check, state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        # Captured Terraform output can contain secrets. Never echo its stdout/stderr automatically.
        if isinstance(exc, subprocess.CalledProcessError):
            print('Command failed (exit ' + str(exc.returncode) + '). Retry the same deployment; consult the setup guide.', file=sys.stderr)
        else:
            print('Setup: ' + str(exc), file=sys.stderr)
        sys.exit(1)
