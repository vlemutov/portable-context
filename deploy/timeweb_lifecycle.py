"""Installation-scoped Timeweb diagnostics and explicit, resumable destruction.

Never enumerate secrets into a support report or infer ownership from a name alone.
"""
import re
import time

from setup import read, save
from timeweb_api import API, TimewebError
from timeweb_setup import digest, target

RESOURCES = {
    'app': ('/api/v1/apps', 'app'),
    'server': ('/api/v1/servers', 'server'),
    'bucket': ('/api/v1/storages/buckets', 'bucket'),
    'ssh_key': ('/api/v1/ssh-keys', 'ssh_key'),
    'git': ('/api/v1/vcs-provider', 'provider'),
    'project': ('/api/v1/projects', 'project'),
}


def load(state, *, allow_transition=False):
    config = read(state/'deployment.json')
    if config.get('mode') != 'timeweb':
        raise ValueError('This command requires a Timeweb installation')
    journal = read(state/'timeweb-journal.json') if (state/'timeweb-journal.json').exists() else {'config': target(config), 'resources': {}}
    if journal['config'] != target(config) and not allow_transition:
        raise ValueError('Installation configuration and journal differ; resume the pending transition first')
    return config, journal


def cleanup_context(state):
    """An explicit destroy may supersede a stalled upgrade, never an unknown identity."""
    config, journal = load(state, allow_transition=True)
    path = state/'timeweb-update.json'
    update = read(path) if path.exists() else None
    pending = update if update and not update.get('complete') else None
    if pending:
        if config not in (pending['before']['deployment.json'], pending['after']['deployment.json']):
            raise ValueError('Deployment differs from pending upgrade')
        # Stages may advance during a resumable memory install, but IDs cannot change.
        candidates = [pending[side]['timeweb-journal.json'] for side in ('before', 'after')]
        if not any(journal['config'] == v['config'] and journal['resources'] == v['resources'] for v in candidates):
            raise ValueError('Resource identities differ from pending upgrade')
    elif journal['config'] != target(config):
        raise ValueError('Installation configuration and journal differ')
    return config, journal, pending


def verify_cleanup(kind, value, config, journal, pending):
    if kind == 'app' and value and pending:
        if value.get('commit_sha') not in (pending['old'], pending['commit']):
            raise ValueError('App source differs from both reviewed upgrade versions')
        config = {**config, 'source_commit': value['commit_sha']}
    verify(kind, value, config, journal)


def complete_list(api, path, key):
    response = api.request(path)
    rows = response[key]
    if not isinstance(rows, list) or int(response.get('meta', {}).get('total', len(rows))) != len(rows):
        raise ValueError('Incomplete inventory; no mutation allowed')
    return rows


def fetch(api, kind, ident):
    if not re.fullmatch(r'[A-Za-z0-9-]+', str(ident)):
        raise ValueError('Invalid resource ID')
    path, key = RESOURCES[kind]
    try:
        response = api.request(path+'/'+str(ident))
        value = response.get(key, response.get('ssh-key'))
        if not isinstance(value, dict):
            raise ValueError('Unexpected resource envelope')
        if str(value.get('id', value.get('provider_id'))) != str(ident):
            raise ValueError('Resource ID mismatch')
        return value
    except TimewebError as error:
        if error.status == 404:
            return None
        raise


def verify(kind, value, config, journal):
    if value is None:
        return
    expected = config['bucket'] if kind == 'bucket' else config['name']+('-edge' if kind == 'app' else '')
    if kind != 'git' and value.get('name') != expected:
        raise ValueError('Resource name differs: '+kind)
    if kind == 'project' and value.get('is_default'):
        raise ValueError('Never delete the default project')
    if kind in ('server', 'bucket'):
        project = config.get('project_id') or journal['resources']['project']['id']
        if value.get('project_id') != project:
            raise ValueError('Resource project differs: '+kind)
    if kind == 'app' and value.get('commit_sha') != config['source_commit']:
        raise ValueError('App version differs; reconcile before cleanup')


def diagnostics(state):
    """Fixed allowlist, no API bodies, log text, env, local paths or credentials."""
    config, journal = load(state, allow_transition=True)
    api = API(state)
    report = {'format': 1, 'provider': 'timeweb', 'at': int(time.time()),
              'source_commit': config.get('source_commit'), 'stage': journal.get('stage', 'not_started'),
              'selection': {k: config.get(k) for k in ('server_preset_id', 'storage_preset_id', 'app_preset_id', 'availability_zone')},
              'resources': {}, 'local': {}, 'configuration_matches_journal': journal['config'] == target(config)}
    for kind, item in journal['resources'].items():
        if kind not in RESOURCES:
            continue
        row = {'id': item.get('id'), 'creation_unresolved': bool(item.get('submitted') and item.get('id') is None)}
        if item.get('id') is not None:
            try:
                live = fetch(api, kind, item['id'])
                row['exists'] = live is not None
                if live:
                    # Status is a machine enum, never arbitrary provider text.
                    status = live.get('status')
                    row['status'] = status if isinstance(status, str) and re.fullmatch('[a-z_]{1,40}', status) else None
                    if kind == 'app':
                        response = api.request('/api/v1/apps/'+str(item['id'])+'/deploys')
                        deploys = response['deploys']
                        row['deploy_history_complete'] = int(response.get('meta', {}).get('total', len(deploys))) == len(deploys)
                        row['deploy_history_returned'] = len(deploys)
                        row['deploy_history_total'] = int(response.get('meta', {}).get('total', len(deploys)))
                        row['deploys'] = [{k: v.get(k) if re.fullmatch(pattern, str(v.get(k, ''))) else None
                                          for k, pattern in (('id', '[A-Za-z0-9-]{1,80}'), ('status', '[a-z_]{1,40}'), ('commit_sha', '[a-f0-9]{40}'))}
                                         for v in deploys]
            except TimewebError as error:
                row.update(http_status=error.status, provider_error_code=error.error_code)
        report['resources'][kind] = row
    for filename in ('installed.json', 'https-installed.json', 'rotation-installed.json', 'owner-enrollment.json', 'timeweb-cleanup.json', 'timeweb-update.json'):
        report['local'][filename] = (state/filename).exists()
    if (state/'https-installed.json').exists():
        receipt = read(state/'https-installed.json')
        report['workspace_id'] = receipt.get('workspace_id')
        report['checks_at_install'] = {k: receipt.get('checks', {}).get(k) is True for k in ('transport', 'oauth_metadata', 'unauthenticated_rejected')}
    error = journal.get('last_error') or {}
    report['last_error'] = {k: error.get(k) for k in ('http_status', 'provider_error_code')}
    report['next'] = 'Resume the same private state. These are infrastructure observations, not proof of client OAuth login or successful import.'
    path = state/'timeweb-support.json'
    save(path, report)
    return {'report': report, 'support_file': str(path), 'contains_resource_ids': True,
            'excluded': 'credentials, keys, environment variables, raw logs, memory contents, recovery files'}


def cleanup_plan(state, api):
    config, journal, pending = cleanup_context(state)
    resources = []
    for kind in ('app', 'server', 'bucket', 'ssh_key', 'git', 'project'):
        item = journal['resources'].get(kind, {})
        if item.get('submitted') and item.get('id') is None:
            raise ValueError('Reconcile ambiguous creation before cleanup: '+kind)
        if item.get('id') is not None:
            live = fetch(api, kind, item['id'])
            verify_cleanup(kind, live, config, journal, pending)
            # Do not include pre-existing projects, keys, or Git connections outside this journal.
            resources.append({'kind': kind, 'id': item['id']})
    ips = complete_list(api, '/api/v1/floating-ips', 'ips')
    server = journal['resources'].get('server', {}).get('id')
    matches = [v for v in ips if config.get('host') and v.get('ip') == config['host']]
    if len(matches) > 1:
        raise ValueError('Ambiguous public IP')
    for value in matches:
        if value.get('resource_id') is not None and str(value['resource_id']) != str(server):
            raise ValueError('Installation IP belongs to another resource')
        resources.insert(2, {'kind': 'ipv4', 'id': value['id'], 'ip': value['ip']})
    review = {'format': 1, 'installation': config['name'], 'binding': digest({'config': config, 'resources': journal['resources']}),
              'abandons_pending_update': pending['operation'] if pending else None,
              'resources': resources, 'irreversible': 'Delete this VM, memory, OAuth accounts, app and S3 including ALL backup versions. Local state and recovery kit are retained; the kit alone cannot restore deleted S3 backups.',
              'preserved': 'Other installations, pre-existing project, account, balance, API token and local files'}
    review['plan_sha256'] = digest(review)
    return review


def cleanup(state, approval=None):
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        api = API(state)
        path = state/'timeweb-cleanup.json'
        saved = read(path) if path.exists() else None
        if saved and approval is None:
            return saved
        if saved:
            review = saved['plan']
        else:
            review = cleanup_plan(state, api)
        if approval is None:
            return review
        if approval != review['plan_sha256'] or digest({k: v for k, v in review.items() if k != 'plan_sha256'}) != approval:
            raise ValueError('Cleanup plan changed')
        config, journal, pending = cleanup_context(state)
        if digest({'config': config, 'resources': journal['resources']}) != review['binding']:
            raise ValueError('Installation changed since cleanup review')
        record = saved or {'plan': review, 'steps': {}, 'complete': False}
        save(path, record)  # tombstone before any DELETE; blocks re-installation
        for item in review['resources']:
            kind, ident = item['kind'], item['id']
            step = record['steps'].setdefault(kind, {})
            if kind == 'ipv4':
                rows = complete_list(api, '/api/v1/floating-ips', 'ips')
                live = next((v for v in rows if str(v['id']) == str(ident)), None)
                if live and (live.get('ip') != item['ip'] or live.get('resource_id')):
                    raise ValueError('IP still bound or changed; resume after VM deletion')
                endpoint = '/api/v1/floating-ips/'+str(ident)
            else:
                live = fetch(api, kind, ident)
                verify_cleanup(kind, live, config, journal, pending)
                endpoint = RESOURCES[kind][0]+'/'+str(ident)
            if live is None:
                step['absent'] = True
                save(path, record)
                continue
            if step.get('submitted'):
                raise ValueError('Deletion still pending or outcome unknown for '+kind+'. Re-run to check absence; no repeated DELETE sent.')
            # Shared metadata must not be removed while it can affect another resource.
            if kind in ('ssh_key', 'git', 'project'):
                servers = complete_list(api, '/api/v1/servers', 'servers')
                apps = complete_list(api, '/api/v1/apps', 'apps')
                buckets = complete_list(api, '/api/v1/storages/buckets', 'buckets')
                if servers or apps or buckets:
                    step['retained'] = 'Other resources exist; metadata may be shared. Review separately.'
                    save(path, record)
                    continue
                if kind == 'project':
                    contents = api.request(endpoint+'/resources')
                    keys = ('servers', 'balancers', 'buckets', 'clusters', 'databases', 'dedicated_servers')
                    if any(contents.get(k) != [] for k in keys) or set(contents)-set(keys)-{'meta', 'response_id'}:
                        step['retained'] = 'Project inventory is not proven empty; review separately.'
                        save(path, record)
                        continue
            step['submitted'] = True
            save(path, record)
            try:
                api.request(endpoint, method='DELETE')
            except TimewebError as error:
                if error.status in (400, 401, 403, 422, 423, 429):
                    step['submitted'] = False
                    save(path, record)
                raise
            # One deletion at a time; observe absence on resume before dependents.
            if kind == 'ipv4':
                absent = not any(str(v['id']) == str(ident) for v in complete_list(api, '/api/v1/floating-ips', 'ips'))
            else:
                absent = fetch(api, kind, ident) is None
            step['absent'] = absent
            save(path, record)
            if not absent:
                return {'pending': kind, 'plan_sha256': approval, 'next': 'Repeat timeweb-cleanup with the same approved hash to observe deletion and continue.'}
        record['complete'] = True
        record['all_planned_resources_absent'] = all(v.get('absent') for v in record['steps'].values())
        save(path, record)
        return record
