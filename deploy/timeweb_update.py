"""Reviewed source upgrade of an existing Timeweb gateway, coordinated with renewal."""
import copy
from contextlib import closing
import json
from pathlib import Path
import re
import shlex
import subprocess

from setup import ROOT, INSTALL_BOOTSTRAP, read, save, checked, ssh_args
from timeweb_api import API, origin
from timeweb_app import current_preset
from timeweb_setup import digest, target

TERMINAL = {'success', 'failed', 'failure', 'error', 'cancelled', 'canceled', 'stopped'}


def remote_step(payload):
    """Runs on the owner VM. Neither credentials nor edge env leave the VM."""
    import sqlite3
    from portable_context.restic_backup import file_lock
    from timeweb_setup import publish
    root = Path(payload['root']).resolve()
    state = root/'gateway-rotation'
    with closing(sqlite3.connect((root/'memory/context.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        workspace = db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
    if workspace != payload['workspace_id']:
        raise ValueError('Wrong memory host')
    with file_lock(state/'rotation.lock'):
        config = read(state/'config.json')
        if config.get('backend') != 'timeweb' or config['app_id'] != payload['app_id']:
            raise ValueError('Wrong renewal controller')
        rotation = read(state/'rotation.json') if (state/'rotation.json').exists() else {}
        if rotation and not rotation.get('complete'):
            raise ValueError('Certificate renewal is in progress; finish it before updating')
        marker = state/'source-update.json'
        pending = read(marker) if marker.exists() else None
        if pending and pending.get('complete') and pending['commit'] != payload['commit']:
            pending = None
        if pending and (pending['commit'] != payload['commit'] or pending['old'] != payload['old']):
            raise ValueError('Another source update is pending')
        if config['source_commit'] not in (payload['old'], payload['commit']):
            raise ValueError('Controller source changed')
        api = API(config['cloud_state'])
        app = api.request('/api/v1/apps/'+str(config['app_id']))['app']
        if app.get('id') != config['app_id'] or app.get('name') != config['app_name'] or origin(app) != 'https://'+config['public_host']:
            raise ValueError('Wrong gateway')
        if app.get('commit_sha') not in (payload['old'], payload['commit']):
            raise ValueError('Unexpected live source')
        if pending and pending.get('complete'):
            if config['source_commit'] != payload['commit'] or app.get('commit_sha') != payload['commit']:
                raise ValueError('Completed update identity differs')
            return {'status': 'complete', 'source_commit': payload['commit'], 'workspace_id': workspace}
        rows = api.request('/api/v1/apps/'+str(config['app_id'])+'/deploys')['deploys']
        if pending is None and any(v.get('status') not in TERMINAL for v in rows):
            raise ValueError('Another app deployment is pending')
        pending = pending or {'old': payload['old'], 'commit': payload['commit'], 'complete': False,
                              'environment_sha256': digest(app.get('envs', {})), 'retry_ids': []}
        if digest(app.get('envs', {})) != pending['environment_sha256']:
            raise ValueError('Gateway environment changed during upgrade')
        publication = state/('source-publish-'+payload['operation']+'.json')
        save(marker, pending)  # timer sees this durable fence between invocations
        if payload.get('retry_failed') and payload['review'] not in pending['retry_ids']:
            if any(v.get('status') not in TERMINAL for v in rows):
                raise ValueError('A deployment is still pending; cannot retry')
            save(publication, {'desired': digest({'env': app['envs'], 'commit': payload['commit']}),
                               'baseline_deploys': [v['id'] for v in rows], 'submitted': False})
            pending['retry_ids'].append(payload['review'])
            save(marker, pending)
        if not publish(api, app, app['envs'], payload['commit'], publication):
            return {'status': 'pending', 'next': 'Repeat the same timeweb-update command; no duplicate deployment is submitted.'}
        current = api.request('/api/v1/apps/'+str(config['app_id']))['app']
        if current.get('commit_sha') != payload['commit'] or current.get('envs') != app['envs']:
            raise ValueError('Deployed gateway differs')
        config['source_commit'] = payload['commit']
        save(state/'config.json', config)
        pending['complete'] = True
        save(marker, pending)
        return {'status': 'complete', 'source_commit': payload['commit'], 'workspace_id': workspace}


REMOTE = '''import sys,json,pathlib,hashlib,subprocess
p=json.load(sys.stdin);root=pathlib.Path(p['root']);folder=root/'gateway-rotation'/'upgrade-helpers'/hashlib.sha256(json.dumps(p['files'],sort_keys=True).encode()).hexdigest()
folder.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,content in p['files'].items():
 path=folder/name
 if pathlib.Path(name).name!=name or not name.endswith('.py'):raise ValueError('Invalid helper name')
 path.write_text(content,encoding='utf-8');path.chmod(0o600)
program="import sys,json;sys.path.insert(0,sys.argv[1]);from timeweb_update import remote_step;print(json.dumps(remote_step(json.load(sys.stdin))))"
r=subprocess.run([str(root/'current/venv/bin/python'),'-c',program,str(folder)],input=json.dumps(p),text=True,capture_output=True)
if r.returncode:raise RuntimeError('Gateway upgrade paused. Same state retained; inspect diagnostics and retry only after reviewing the deployment.')
sys.stdout.write(r.stdout)
'''


def call_remote(state, payload):
    names = ('timeweb_update.py', 'timeweb_setup.py', 'timeweb_api.py', 'timeweb_app.py', 'timeweb_vm.py', 'setup.py', 'cloud_gateway.py', 'gateway.py')
    payload = {**payload, 'files': {n: (ROOT/'deploy'/n).read_text(encoding='utf-8') for n in names}}
    result = checked(ssh_args(state, read(state/'deployment.json'))+['python3 -c '+shlex.quote(INSTALL_BOOTSTRAP)],
                     input=json.dumps({'script': REMOTE, 'payload': payload}), text=True, capture_output=True, encoding='utf-8', timeout=180)
    return json.loads(result.stdout)


def checkout(commit):
    actual = checked(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if actual != commit:
        raise ValueError('Use a full checkout of the requested commit before updating')
    checked(['git', '-C', str(ROOT), 'diff', '--quiet', 'HEAD', '--'])
    untracked = checked(['git', '-C', str(ROOT), 'ls-files', '--others', '--exclude-standard', 'deploy', 'src'], capture_output=True, text=True).stdout
    if untracked.strip():
        raise ValueError('Untracked installation code; use a clean checkout')


def update(state, commit, approval=None, retry_failed=False):
    from portable_context.restic_backup import file_lock
    from timeweb_lifecycle import load, fetch, verify
    from timeweb_setup import install_locked
    from https_setup import check_public
    from rotation_setup import refresh
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Use a full published source commit')
    checkout(commit)
    with file_lock(state/'timeweb-install.lock'):
        if (state/'timeweb-cleanup.json').exists():
            raise ValueError('This installation is being deleted or retired')
        path = state/'timeweb-update.json'
        pending = read(path) if path.exists() else None
        if pending and pending.get('complete') and pending['commit'] != commit:
            save(state/('timeweb-update-'+pending['operation']+'.json'), pending)
            pending = None
        if pending and pending['commit'] != commit:
            raise ValueError('Resume the pending source update first')
        if pending and pending.get('complete'):
            return {'updated': True, 'already_complete': True, 'source_commit': commit, 'workspace_id': pending['workspace_id']}
        api = API(state)
        if pending is None:
            config, journal = load(state)
            if not (state/'rotation-installed.json').exists() or not (state/'https-installed.json').exists():
                raise ValueError('Finish initial HTTPS installation before changing source')
            if config['source_commit'] == commit:
                return {'unchanged': True, 'next': 'Use update to refresh memory and controller from this pinned checkout.'}
            app = fetch(api, 'app', journal['resources']['app']['id'])
            if app is None:
                raise ValueError('App missing; no replacement will be created')
            verify('app', app, config, journal)
            installed = read(state/'installed.json')
            # Keep the original creation request hash in step with the source pin.
            repo = app.get('repository_id') or app.get('repository', {}).get('id')
            body = {'name': config['name']+'-edge', 'comment': 'Portable Context HTTPS gateway', 'type': 'backend',
                    'language': 'docker', 'framework': 'docker', 'provider_id': journal['resources']['git']['id'],
                    'repository_id': repo, 'branch_name': config['source_branch'], 'commit_sha': config['source_commit'],
                    'build_cmd': '', 'run_cmd': '', 'is_auto_deploy': False, 'preset_id': current_preset(config, journal),
                    'build_entry_path': '/deploy/timeweb-edge', 'health_check': '/_pc/health', 'envs': {}}
            if journal['resources']['app']['body_sha256'] != digest(body):
                raise ValueError('App creation identity differs')
            after_config = {**config, 'source_commit': commit}
            after_journal = copy.deepcopy(journal)
            after_journal['config'] = target(after_config)
            body['commit_sha'] = commit
            after_journal['resources']['app']['body_sha256'] = digest(body)
            plan = read(state/'timeweb-plan.json')
            if plan['config'] != target(config):
                raise ValueError('Infrastructure plan differs')
            plan['config'] = target(after_config)
            plan['plan_sha256'] = digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
            files = {'deployment.json': after_config, 'timeweb-journal.json': after_journal,
                     'timeweb-plan.json': plan, 'timeweb-approved.json': {'plan_sha256': plan['plan_sha256']}}
            pending = {'commit': commit, 'old': config['source_commit'], 'app_id': app['id'], 'root': installed['root'],
                       'workspace_id': installed['memory']['workspace_id'], 'origin': origin(app),
                       'before': {n: read(state/n) for n in files}, 'after': files, 'complete': False}
            pending['operation'] = digest({'app': app['id'], 'old': pending['old'], 'new': commit})[:24]
        rows = api.request('/api/v1/apps/'+str(pending['app_id'])+'/deploys')['deploys']
        preview = {'operation': pending['operation'], 'app_id': pending['app_id'], 'workspace_id': pending['workspace_id'],
                   'old_commit': pending['old'], 'new_commit': commit, 'retry_failed': retry_failed,
                   'effect': 'Update the SAME gateway, memory and renewal controller. Keep DNS, workspace, VM, S3 and live certificate environment. Retain resources on failure.'}
        if retry_failed:
            preview['deploys'] = [{k: row.get(k) for k in ('id', 'status', 'commit_sha')} for row in rows]
        preview['plan_sha256'] = digest(preview)
        if approval is None:
            return preview
        if approval != preview['plan_sha256']:
            raise ValueError('Update review changed')
        if not pending.get('files_committed'):
            for name, before in pending['before'].items():
                if read(state/name) not in (before, pending['after'][name]):
                    raise ValueError('Local installation changed: '+name)
        elif read(state/'deployment.json') != pending['after']['deployment.json']:
            raise ValueError('Deployment changed during memory upgrade')
        save(path, pending)
        if not pending.get('controller_prepared'):
            # Install the fence-aware timer code before creating a remote update intent.
            refresh(state)
            pending['controller_prepared'] = True
            save(path, pending)
        if not pending.get('edge_complete'):
            result = call_remote(state, {k: pending[k] for k in ('root', 'workspace_id', 'app_id', 'old', 'commit', 'operation')} |
                                 {'retry_failed': retry_failed, 'review': approval})
            if result['status'] != 'complete':
                return {**result, 'plan_sha256': approval}
            check_public(pending['origin'], 120)
            pending['edge_complete'] = True
            save(path, pending)
        if not pending.get('files_committed'):
            for name, value in pending['after'].items():
                save(state/name, value)
            pending['files_committed'] = True
            save(path, pending)
        # This is the existing backup/restore-checked memory updater and public check.
        result = install_locked(state, target(read(state/'deployment.json')))
        if result['workspace_id'] != pending['workspace_id']:
            raise ValueError('Workspace changed after update')
        pending['complete'] = True
        save(path, pending)
        return {'updated': True, 'workspace_id': pending['workspace_id'], 'source_commit': commit, 'checks': result['checks']}
