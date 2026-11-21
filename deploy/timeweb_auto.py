"""One reviewed, bounded VM search. Stop at the first allocated VM; never delete."""
import copy
import math
import time
import uuid

from setup import read, save
from timeweb_api import API, TimewebError
import timeweb_setup as tw
from timeweb_selection import options
from timeweb_vm import is_vm_rejection, require_selection_idle

FILES = ('deployment.json', 'timeweb-journal.json', 'timeweb-plan.json')


class SearchStop(ValueError):
    """Allowlisted diagnostic reason, never a provider response body."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def snapshot(state):
    return {name: read(state/name) if (state/name).exists() else None for name in FILES}


def context(state):
    config = read(state/'deployment.json')
    if config.get('mode') != 'timeweb':
        raise ValueError('Automatic selection requires Timeweb')
    for name in ('timeweb-cleanup.json', 'timeweb-update.json', 'timeweb-vm-selection.json'):
        if (state/name).exists() and (name == 'timeweb-cleanup.json' or not read(state/name).get('complete')):
            raise ValueError('Finish the existing transition before VM selection; retired state cannot be reused')
    j = read(state/'timeweb-journal.json') if (state/'timeweb-journal.json').exists() else {
        'config': tw.target(config), 'resources': {}, 'stage': 'not_started'}
    if j['config'] != tw.target(config):
        raise ValueError('Deployment and resource journal differ')
    if config.get('host') or j.get('server_ipv4') or set(j['resources']) - {'project', 'ssh_key', 'server'}:
        raise ValueError('Installation has advanced beyond VM selection; do not replace its server')
    return config, j


def money(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Prices must be finite and nonnegative')
    return value


def unused_intent(server):
    return not any(server.get(k) for k in ('id','submitted','submitted_at','creation_rejection','creation_attempts'))


def record_rejection(attempt, rejection):
    attempt.update(status='rejected', http_status=rejection.get('status'),
                   error_code=rejection.get('error_code'), response_id=rejection.get('response_id'))


def prepare(state, locations, max_monthly_total, monthly_extras, max_attempts=6, timeout=300, families=None, zones=None):
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        require_selection_idle(state)
        config, j = context(state)
        server = j['resources'].get('server', {})
        if server.get('id') or server.get('submitted'):
            raise ValueError('VM exists or creation outcome is unknown; reconcile before any search')
        rejection = server.get('creation_rejection')
        if not unused_intent(server) and (not rejection or not is_vm_rejection(rejection.get('status'), rejection.get('error_code'))):
            raise ValueError('Resolve the current VM error before automatic selection')
        if not locations or len(set(locations)) != len(locations):
            raise ValueError('Specify distinct Russian locations in preference order')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20 or type(timeout) is not int or not 30 <= timeout <= 1800:
            raise ValueError('Use 1..20 attempts and 30..1800 seconds')
        total, extras = money(max_monthly_total), money(monthly_extras)
        data = tw.catalog(state)
        base = tw.plan(state, persist=False, catalogs=data)
        fixed = sum(money(base['presets'][k]['price']) for k in ('apps', 'storage'))
        vm_ceiling = total - fixed - extras
        if vm_ceiling <= 0:
            raise ValueError('Total ceiling leaves no budget for a VM after App, S3 and reviewed extras')
        found = options(state, locations, vm_ceiling, data=data)
        rows = sorted(found['options'], key=lambda v: (locations.index(v['location']), v['price'], v['id']))
        candidates = []
        for row in rows:
            if families and row['family'] not in families:
                continue
            for zone in row['availability_zones']:
                if zones and zone not in zones:
                    continue
                candidate = tw.plan(state, config={**config, 'server_preset_id': row['id'], 'availability_zone': zone}, persist=False, catalogs=data)
                estimated = sum(money(candidate['presets'][k]['price']) for k in ('servers', 'apps', 'storage')) + extras
                if estimated > total:
                    raise ValueError('Catalog prices changed while preparing the search; review again')
                candidates.append({'preset_id': row['id'], 'zone': zone, 'location': row['location'],
                    'family': row['family'], 'estimated_monthly_total': round(estimated, 2), 'plan': candidate})
                if len(candidates) == max_attempts:
                    break
            if len(candidates) == max_attempts:
                break
        if not candidates:
            raise ValueError('No untried candidates within reviewed locations, hardware and total price. Review bounds; no resource created.')
        review = {'format': 1, 'review_id': str(uuid.uuid4()), 'initial': snapshot(state), 'locations': locations, 'families': families or [], 'zones': zones or [],
            'max_monthly_total': total, 'monthly_extras': extras, 'max_attempts': max_attempts,
            'timeout_seconds': timeout, 'candidates': candidates, 'excluded': found['excluded'],
            'payment': 'Review autopay before approval: this is an estimated recurring total, not a card debit or enforced spending cap. Extras must cover IPv4 and expected variable charges.',
            'effect': 'Sequential real creation attempts, one VM retained at first success. Only project/key/VM are provisioned here. No automatic deletion or trial servers. Unknown outcomes stop the search.'}
        review['plan_sha256'] = tw.digest(review)
        save(state/'timeweb-auto-plan.json', review)
        return review


def no_matching_vm(api, name):
    value = api.request('/api/v1/servers')
    rows = value['servers']
    if not isinstance(rows, list) or any(type(n) is not int or n != len(rows)
        for k, n in value.get('meta', {}).items() if k in ('total', 'amount')):
        raise SearchStop('incomplete_vm_inventory')
    if any(v.get('name') == name for v in rows):
        raise SearchStop('matching_vm_requires_reconciliation')


def transition(state, run):
    pending = run.get('transition')
    if not pending:
        return
    current = snapshot(state)
    for name in FILES:
        if current[name] not in (pending['before'][name], pending['after'][name]):
            raise ValueError('State changed during automatic selection; preserve it for diagnosis')
    for name in FILES:
        save(state/name, pending['after'][name])
    run.pop('transition')
    save(state/'timeweb-auto-run.json', run)


def finish(state, run, status, reason, **details):
    run.update(status=status, reason=reason, **details)
    if status != 'selected':
        run['next'] = 'Inspect install-status and timeweb-diagnostics; resources are retained. Resolve unknown creation outcomes before another search. A new search requires a new reviewed plan.'
    save(state/'timeweb-auto-run.json', run)
    return run


def execute(state, approval, payment_reviewed=False):
    from portable_context.restic_backup import file_lock
    with file_lock(state/'timeweb-install.lock'):
        review = read(state/'timeweb-auto-plan.json')
        expected = tw.digest({k: v for k, v in review.items() if k != 'plan_sha256'})
        if approval != expected or review['plan_sha256'] != expected:
            raise ValueError('Review and approve the exact automatic selection plan')
        path = state/'timeweb-auto-run.json'
        run = read(path) if path.exists() else None
        if run and run['plan_sha256'] == expected and run['status'] != 'running':
            return run  # Exhaustion/fatal errors never silently start a new search.
        from timeweb_payment import require
        require(state,'auto')
        if not run or run['plan_sha256'] != expected:
            if run and run['status'] == 'running':
                raise ValueError('Finish the previous automatic search first')
            if snapshot(state) != review['initial']:
                raise ValueError('Installation changed after preview; prepare a new plan')
            context(state)
            run = {'plan_sha256': expected, 'status': 'running', 'started_at': time.time(),
                'deadline': time.time() + review['timeout_seconds'], 'payment_reviewed': True,
                'attempts': [], 'index': 0}
            save(path, run)
        try:
            transition(state, run)
            while run['index'] < len(review['candidates']):
                index = run['index']
                candidate = review['candidates'][index]
                config, j = context(state)
                active = len(run['attempts']) > index
                if not active:
                    if time.time() >= run['deadline']:
                        return finish(state, run, 'stopped', 'time_limit')
                    if index:
                        time.sleep(min(2, max(0, run['deadline'] - time.time())))
                    no_matching_vm(API(state), config['name'])
                    server = j['resources'].get('server', {})
                    if server.get('id') or server.get('submitted'):
                        return finish(state, run, 'stopped', 'reconcile_vm_before_retry')
                    updated = copy.deepcopy(j)
                    rejection = server.get('creation_rejection')
                    if not unused_intent(server) and (not rejection or not is_vm_rejection(rejection.get('status'), rejection.get('error_code'))):
                        return finish(state, run, 'stopped', 'unrecognized_vm_failure')
                    if rejection:
                        updated.setdefault('vm_selection_history', []).append({'previous_server': server,
                            'rejection': rejection, 'automatic_plan': expected,
                            'rejected_selection': {'preset_id': config['server_preset_id'], 'availability_zone': config['availability_zone']}})
                    updated['resources'].pop('server', None)
                    updated.pop('last_error', None)
                    selected = candidate['plan']['config']
                    updated['config'] = tw.target(selected)
                    run['attempts'].append({'preset_id': candidate['preset_id'], 'zone': candidate['zone'], 'status': 'pending'})
                    run['transition'] = {'before': snapshot(state), 'after': {'deployment.json': selected,
                        'timeweb-journal.json': updated, 'timeweb-plan.json': candidate['plan']}}
                    save(path, run)
                    transition(state, run)
                    config, j = context(state)
                if config != candidate['plan']['config'] or read(state/'timeweb-plan.json') != candidate['plan']:
                    raise ValueError('Selected VM configuration differs from the approved candidate')
                server = j['resources'].get('server', {})
                rejection = server.get('creation_rejection', {})
                # Recover a rejection recorded just before controller interruption.
                if not server.get('id') and not server.get('submitted') and is_vm_rejection(rejection.get('status'), rejection.get('error_code')):
                    record_rejection(run['attempts'][index], rejection)
                    run['index'] += 1
                    save(path, run)
                    continue
                if not server.get('id') and server.get('submitted'):
                    return finish(state, run, 'stopped', 'creation_outcome_unknown')
                if not server.get('id'):
                    if time.time() >= run['deadline']:
                        return finish(state, run, 'stopped', 'time_limit')
                    fresh = tw.plan(state, config=config, persist=False)
                    if fresh != candidate['plan']:
                        return finish(state, run, 'stopped', 'catalog_or_price_changed')
                    no_matching_vm(API(state), config['name'])
                def before_create():
                    if time.time() >= run['deadline']:
                        raise SearchStop('time_limit')
                    run['attempts'][index]['submitted_at'] = time.time()
                    save(path, run)
                try:
                    result = tw.provision(state, config, vm_only=True, before_vm_create=before_create)
                except TimewebError as error:
                    live = read(state/'timeweb-journal.json')['resources'].get('server', {})
                    rejected = live.get('creation_rejection', {})
                    if (not live.get('id') and not live.get('submitted') and error.method == 'POST'
                        and is_vm_rejection(error.status, error.error_code)
                        and rejected.get('status') == error.status and rejected.get('error_code') == error.error_code):
                        record_rejection(run['attempts'][index], rejected)
                        run['index'] += 1
                        save(path, run)
                        continue
                    return finish(state, run, 'stopped', 'provider_error', http_status=error.status,
                                  provider_error_code=error.error_code, response_id=error.response_id)
                run['attempts'][index]['status'] = 'allocated'
                return finish(state, run, 'selected', 'first_vm_allocated', selected=result,
                    next='Review selected receipt, then apply --approve-plan '+candidate['plan']['plan_sha256']+' and install --https on the SAME state. Allocation is not OS or memory readiness.')
            return finish(state, run, 'exhausted', 'approved_candidates_exhausted')
        except SearchStop as error:
            return finish(state, run, 'stopped', error.reason)
        except TimewebError as error:
            return finish(state, run, 'stopped', 'provider_error', http_status=error.status,
                          provider_error_code=error.error_code, response_id=error.response_id)
        except (ValueError, KeyError, TypeError) as error:
            # No exception body: it may contain provider credentials or local secrets.
            return finish(state, run, 'stopped', 'diagnosis_required', error_type=type(error).__name__)
