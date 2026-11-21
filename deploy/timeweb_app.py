"""Recover App Platform capacity refusals, including journals written by preview.6."""
from copy import deepcopy

from setup import read, save
from timeweb_api import API


def rejected(item):
    """Require one matching refusal for every submission, including consumed retries."""
    if item.get('path') != '/api/v2/apps' or item.get('key') != 'app' or item.get('id') is not None:
        return False
    attempts = item.get('creation_attempts', [])
    submissions = [row.get('previous', {}) for row in item.get('retry_history', [])] + [item]
    if len(attempts) != len(submissions):
        return False
    for index, (attempt, previous) in enumerate(zip(attempts, submissions)):
        if (attempt.get('status') != 409 or attempt.get('error_code') != 'no_free_node'
                or previous.get('submitted_at') is None or attempt.get('submitted_at') != previous['submitted_at']
                or previous.get('body_sha256') != item.get('body_sha256')
                or previous.get('creation_attempts') != attempts[:index + 1]):
            return False
    return True


GUIDANCE = 'App capacity rejected. Review timeweb-select-app or preview timeweb-cleanup in the same state.'


def current_preset(config, journal):
    return journal.get('app_selection', {}).get('preset', {}).get('id', config['app_preset_id'])


def options(api):
    rows = api.request('/api/v1/presets/apps')['backend_presets']
    choices = [{k: row[k] for k in ('id','location','price','cpu','ram','disk') if k in row}
               for row in rows if str(row.get('location', '')).startswith(('ru','msk','spb'))
               and isinstance(row.get('price'), (int, float)) and row['price'] >= 0]
    return sorted(choices, key=lambda row: (row['price'], row['id']))


def check_selection(api, config, journal):
    from timeweb_retry import inventory
    inventory(api, config, journal)
    preset = journal['app_selection']['preset']
    if preset not in options(api):
        raise ValueError('Selected backend tariff changed; review a fresh price before ordering')


def select(state, preset_id=None, approval=None):
    from portable_context.restic_backup import file_lock
    from timeweb_lifecycle import load
    from timeweb_retry import inventory
    from timeweb_setup import digest
    with file_lock(state/'timeweb-install.lock'):
        if (state/'timeweb-cleanup.json').exists():
            raise ValueError('Installation retired or being deleted')
        config, journal = load(state)
        item = journal['resources'].get('app', {})
        # An approved selection is still replaceable until its first submission.
        waiting = ('app_selection' in journal and item.get('id') is None
                   and not item.get('submitted_at') and not item.get('submitted'))
        if not rejected(item) and not waiting:
            raise ValueError('Only confirmed app capacity refusals allow changing the preset')
        api = API(state)
        inventory(api, config, journal)
        choices = options(api)
        current = current_preset(config, journal)
        if preset_id is None:
            if approval: raise ValueError('Select a preset before approval')
            return {'current_preset_id': current, 'options': choices, 'capacity': 'unknown', 'price_unit': 'RUB/month'}
        preset = next((row for row in choices if row['id'] == preset_id), None)
        if not preset:
            raise ValueError('Select a backend preset from the current Russian catalog')
        review = {'action': 'select-app-preset', 'installation': config['name'], 'previous_preset_id': current,
                  'preset': preset, 'state_sha256': digest(journal), 'price_unit': 'RUB/month',
                  'effect': 'Resume HTTPS on the existing VM and bucket with this app tariff.'}
        review['plan_sha256'] = digest(review)
        if approval is None: return review
        if approval != review['plan_sha256']: raise ValueError('App selection changed; review again')
        journal.setdefault('app_selection_history', []).append({'preset_id': current, 'attempt': deepcopy(item)})
        journal['app_selection'] = review
        journal['resources'].pop('app', None)
        save(state/'timeweb-journal.json', journal)
        return {'selected': preset, 'next': 'Resume install --https with the same state.'}
