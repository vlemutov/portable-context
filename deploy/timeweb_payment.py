"""Plan-bound owner approval for new Timeweb charges. All receipts stay in private state."""
import math
import time

from setup import read, save
from timeweb_api import API, TimewebError


class PaymentReviewRequired(ValueError):
    """A new paid order lacks matching approval in this installation."""


def amount(value):
    if isinstance(value, bool):
        raise ValueError('Expected a finite nonnegative price')
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError('Expected a finite nonnegative price')
    return number


def plans(state, scope):
    from timeweb_setup import digest
    filename = 'timeweb-auto-plan.json' if scope == 'auto' else 'timeweb-plan.json'
    plan = read(state / filename)
    if digest({k:v for k,v in plan.items() if k != 'plan_sha256'}) != plan['plan_sha256']:
        raise ValueError('Plan changed. Generate a new plan before reviewing costs.')
    if scope == 'auto':
        return plan, [c['plan'] for c in plan['candidates']]
    return plan, [plan]


def selection(state):
    path = state / 'timeweb-journal.json'
    return read(path).get('app_selection', {}) if path.exists() else {}


def account(state):
    try:
        data = API(state).request('/api/v1/account/finances')['finances']
    except (TimewebError, KeyError, TypeError):
        return {'available': False}
    if not isinstance(data, dict):
        return {'available': False}
    result = {'available': True, 'autopay_card_reported': True if data.get('autopay_card_info') else None}
    if isinstance(data.get('currency'), str) and data['currency'].upper() in ('RUB','USD','EUR','KZT'):
        result['currency'] = data['currency'].upper()
    for key in ('balance','monthly_fee','hourly_fee','hours_left'):
        try:
            value = float(data[key])
            if math.isfinite(value):
                result[key] = value
        except (KeyError, ValueError, TypeError):
            pass
    return result


def review(state, scope, hours=None, extras=None, autopay=None, approval=None):
    from portable_context.restic_backup import file_lock
    from timeweb_setup import digest
    with file_lock(state/'timeweb-install.lock'):
        plan, candidates = plans(state, scope)
        app = selection(state)
        draft = state/'timeweb-payment-plan.json'
        binding = {'scope':scope, 'plan_sha256':plan['plan_sha256'],
                   'app_selection_sha256':app.get('plan_sha256')}
        if approval:
            preview = read(draft)
            if (digest(preview) != approval or preview['binding'] != binding
                    or any(v is not None for v in (hours,extras,autopay))):
                raise ValueError('Payment preview changed. Review the current terms before approval.')
            receipt = {'approved_at':int(time.time()), 'approval_sha256':approval, 'terms':preview}
            save(state/'timeweb-payment-approved.json',receipt)
            return {'approved':approval, 'binding':binding}
        if autopay not in ('enabled','disabled','unknown') or hours is None or amount(hours) <= 0:
            raise ValueError('Specify positive --hours and the reviewed --autopay status; use unknown if unavailable')
        if scope == 'auto':
            if extras is not None and extras != plan['monthly_extras']:
                raise ValueError('Use the extras already included in the automatic plan')
            extras = plan['monthly_extras']
        if extras is None:
            raise ValueError('Specify --monthly-extras for IPv4 and expected metered charges')
        extras = amount(extras)
        totals = []
        for c in candidates:
            prices = c['presets']
            total = sum(amount(p['price']) for p in (prices['servers'],prices['storage'],app.get('preset',prices['apps']))) + extras
            totals.append(amount(round(total,2)))
        finances = account(state)
        if finances.get('currency') not in (None,'RUB'):
            raise ValueError('Review account currency before using RUB estimates.')
        # Existing account fees may already include this partial installation.
        journal_path = state/'timeweb-journal.json'
        resources = read(journal_path)['resources'] if journal_path.exists() else {}
        existing_order = any(resources.get(key,{}).get('submitted') or resources.get(key,{}).get('id')
                             for key in ('server','app','bucket'))
        month_after_order = None
        if not existing_order and finances.get('currency') == 'RUB' and 'monthly_fee' in finances:
            month_after_order = [round(finances['monthly_fee']+n,2) for n in (min(totals),max(totals))]
        preview = {'binding':binding, 'covered_install_plans':[c['plan_sha256'] for c in candidates],
                   'autopay_owner_reported':autopay, 'account_snapshot':finances,
                   'estimated_account_month_after_order_rub':month_after_order,
                   'monthly_extras_rub':extras, 'estimated_monthly_rub':[min(totals),max(totals)],
                   'hours':hours, 'estimated_test_rub':[round(amount(n*hours/730),2) for n in (min(totals),max(totals))],
                   'card_debit_limit':None,
                   'payment_note':'Account top-up can cover a month of all services. Actual bank debit and variable usage may differ from this installation estimate.'}
        save(draft,preview)
        return {**preview, 'approval_sha256':digest(preview)}


def require(state, scope='install'):
    from timeweb_setup import digest
    path = state/'timeweb-payment-approved.json'
    receipt = read(path) if path.exists() else {}
    terms = receipt.get('terms', {})
    binding = terms.get('binding', {})
    plan, _ = plans(state, scope)
    wanted = plan['plan_sha256']
    covered = wanted in terms.get('covered_install_plans',[]) if scope == 'install' else (
        binding.get('scope') == 'auto' and wanted == binding.get('plan_sha256'))
    if covered and scope == 'install' and binding.get('scope') == 'auto':
        path = state/'timeweb-auto-run.json'
        run = read(path) if path.exists() else {}
        active = plan['config']
        selected = run.get('selected', {})
        covered = (run.get('status') == 'selected' and run.get('plan_sha256') == binding['plan_sha256']
                   and selected.get('preset_id') == active['server_preset_id']
                   and selected.get('availability_zone') == active['availability_zone'])
    if (not covered or receipt.get('approval_sha256') != digest(terms)
            or binding.get('app_selection_sha256') != selection(state).get('plan_sha256')):
        raise PaymentReviewRequired('Payment review required before ordering. Preview timeweb-payment --scope '+scope+
                         ', show costs and autopay risk, then approve the exact preview with the owner.')


def check_price(api, state, kind):
    from timeweb_setup import find_row
    plan = read(state/'timeweb-plan.json')
    expected = plan['presets'][kind]
    if kind == 'apps':
        expected = selection(state).get('preset', expected)
    endpoint = 'storages' if kind == 'storage' else kind
    live = find_row(api.request('/api/v1/presets/'+endpoint), expected['id'])
    if not live or live.get('price') != expected['price']:
        raise ValueError('Tariff changed before order. Review the current tariff and payment terms.')
