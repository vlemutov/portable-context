"""Explicit, single-use permission to retry an unresolved app creation."""
import copy
import time

from setup import read, save
from timeweb_api import API
from timeweb_app import rejected, GUIDANCE
from timeweb_lifecycle import complete_list, load
from timeweb_setup import digest


def inventory(api, config, journal, known_id=None):
    rows = complete_list(api, '/api/v1/apps', 'apps')
    provider = journal['resources']['git']['id']
    candidates = [v for v in rows if v.get('name') == config['name']+'-edge'
                  or str(v.get('provider_id')) == str(provider)]
    if candidates and (known_id is None or any(str(v.get('id')) != str(known_id) for v in candidates)):
        raise ValueError('Possible late-created or duplicate app found. Inspect and reconcile its ID; nothing deleted.')
    return sorted([{'id': v['id'], 'name': v.get('name'), 'provider_id': v.get('provider_id'),
                    'repository_id': v.get('repository_id')} for v in rows], key=lambda v: str(v['id']))


def retry_create(state, approval=None):
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        config, journal = load(state)
        if (state/'timeweb-cleanup.json').exists():
            raise ValueError('Installation retired or being deleted')
        if (state/'timeweb-update.json').exists() and not read(state/'timeweb-update.json').get('complete'):
            raise ValueError('Resume pending source update first')
        item = journal['resources'].get('app', {})
        if rejected(item):
            raise ValueError(GUIDANCE)
        if item.get('id') is not None or not item.get('submitted') or item.get('path') != '/api/v2/apps' or item.get('key') != 'app':
            raise ValueError('No unresolved app creation to retry')
        rows = inventory(API(state), config, journal)
        plan = {'action': 'retry-app-create-once', 'config': journal['config'], 'attempt': item,
                'resources': {k: v.get('id') for k, v in journal['resources'].items()}, 'inventory': rows,
                'risk': 'The original request may still finish late. A duplicate remains possible and will not be deleted automatically.',
                'next': 'Resume install --https on the SAME state; one POST is authorized, after a fresh inventory check.'}
        plan['plan_sha256'] = digest(plan)
        if approval is None:
            return plan
        if approval != plan['plan_sha256']:
            raise ValueError('Retry plan changed; review again')
        item['retry_permit'] = {'plan_sha256': approval, 'body_sha256': item['body_sha256'],
                                'approved_at': int(time.time()), 'inventory': rows}
        save(state/'timeweb-journal.json', journal)
        return {'approved': approval, 'next': plan['next'], 'risk': plan['risk']}


def before_create(journal, api, item):
    permit = item.get('retry_permit')
    if not permit:
        raise ValueError('Creation outcome unknown for app. Reconcile an existing ID or review timeweb-retry-create.')
    if permit['body_sha256'] != item['body_sha256']:
        raise ValueError('Approved app request changed')
    if time.time() - permit['approved_at'] > 900:
            raise ValueError('Retry approval expired; review timeweb-retry-create again')
    rows = inventory(api, journal.value['config'], journal.value)
    if rows != permit['inventory']:
        raise ValueError('App inventory changed since approval; no POST sent')
    previous = copy.deepcopy({k: v for k, v in item.items() if k not in ('retry_history', 'retry_permit')})
    item.setdefault('retry_history', []).append({'previous': previous, 'approval': permit,
                                                'consumed_at': int(time.time())})
    item.pop('retry_permit')
    # Persist consumption BEFORE POST. A crash here requires a new explicit review.
    journal.save()
