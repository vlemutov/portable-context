"""Read Cloud.ru Evolution v3 quotas. No quota/permission changes and no default limits.

Contract: https://cloud.ru/docs/administration/ug/topics/api-ref__administration_op-v3
An allocation is not a guarantee of physical capacity. Unknown is never 'passed'.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
import time

from cloud_gateway import CloudAPI, CloudAPIError
from setup import read, save


def pages(api, path, field, query):
    result=[];seen=set();query=dict(query)
    for _ in range(50):
        value=api.organization(path,query)
        if not isinstance(value,dict) or not isinstance(value.get(field),list):
            raise ValueError('Unrecognized quota API response')
        result.extend(value[field])
        token=value.get('next_page_token')
        if not token:return result
        if not isinstance(token,str) or token in seen:raise ValueError('Incomplete quota pagination')
        seen.add(token);query['page_token']=token
    raise ValueError('Quota pagination limit reached; manual review required')


def finite(value):
    try:
        number=Decimal(str(value))
        return number if number.is_finite() and number>=0 else None
    except InvalidOperation:return None


def evaluate(rows, requirements, scopes):
    """Requirements use actual API codes and units, selected from the reviewed plan.

    Negative/unset limits or missing ancestor usage need manual interpretation.
    A new separate project still consumes the organization's quota.
    """
    checks=[]
    for code,amount in requirements.items():
        demand=finite(amount)
        if demand is None:raise ValueError('Quota demand must be a finite nonnegative amount')
        balances=[]
        for scope,identifier in scopes:
            matches=[r for r in rows if r.get('resource_type_code')==code and
                     r.get('subject_type_code')==scope and r.get('subject_id')==identifier]
            if len(matches)!=1:break
            row=matches[0];limit=finite(row.get('individual_limit'))
            used=finite(row.get('aggregated_usage'))
            if limit is None or used is None:break
            balances.append(max(Decimal(0),limit-used))
        available=min(balances) if len(balances)==len(scopes) and balances else None
        checks.append({'resource_type_code':code,'additional_required':str(demand),
            'available':str(available) if available is not None else None,
            'status':'unknown' if available is None else ('sufficient' if demand<=available else 'insufficient')})
    return checks


def check(state, requirements=None):
    state=Path(state);config=read(state/'deployment.json')
    if config.get('mode')!='cloudru':return {'status':'not_applicable'}
    if requirements is not None and (not isinstance(requirements,dict) or not requirements):
        raise ValueError('Quota requirements must be a nonempty mapping of real API codes to additional amounts')
    if requirements and any(finite(v) is None for v in requirements.values()):
        raise ValueError('Quota requirements must contain finite nonnegative amounts')
    project=config['project_id'];result={'project_id':project,'checked_at':time.time(),
        'status':'unknown','capacity':'Not guaranteed by quota availability',
        'next':'Review additional resources in the plan against quota-check rows. If API access is unavailable, use Cloud.ru Administration > Quotas for the organization and project; no credentials or state need to be shared.'}
    try:
        api=CloudAPI(state,project)
        details=api.organization('/v3/projects/'+project)
        if details.get('id')!=project:raise ValueError('Quota API returned another project')
        scopes=[('QUOTA_SUBJECT_TYPE_PROJECT',project)]
        for key,scope in [('organization_unit_id','QUOTA_SUBJECT_TYPE_ORGANIZATION_UNIT'),('customer_id','QUOTA_SUBJECT_TYPE_CUSTOMER')]:
            if details.get(key):scopes.append((scope,details[key]))
        # Missing ancestors cannot prove sufficient organization-wide capacity.
        complete=len(scopes)==3
        types=api.organization('/v3/quotas/resourceTypes').get('resource_types')
        if not isinstance(types,list):raise ValueError('Unrecognized quota resource types')
        rows=[]
        for scope,identifier in scopes:
            rows+=pages(api,'/v3/quotas/usages','usages',{'subject_type_codes':[scope],'subject_ids':[identifier],'page_size':100})
        allowed={'resource_type_code','subject_type_code','subject_id','individual_limit','individual_usage','aggregated_usage'}
        rows=[{k:v for k,v in r.items() if k in allowed} for r in rows if isinstance(r,dict) and (r.get('subject_type_code'),r.get('subject_id')) in scopes]
        result.update(rows=rows,resource_types=[{k:v for k,v in r.items() if k in ('code','name','unit','service_code','service_name')} for r in types if isinstance(r,dict)],
                      status='review_required',scopes_complete=complete)
        if requirements:
            checks=evaluate(rows,requirements,scopes) if complete else [{'status':'unknown'}]
            result['checks']=checks
            result['status']='insufficient' if any(c['status']=='insufficient' for c in checks) else ('sufficient_for_selected_requirements' if all(c['status']=='sufficient' for c in checks) else 'unknown')
    except (CloudAPIError,ValueError,OSError,KeyError) as error:
        result['reason']=error.reason if isinstance(error,CloudAPIError) else 'quota_read_unavailable_or_schema_changed'
        if isinstance(error,CloudAPIError):result['http_status']=error.status
    if result.get('http_status') == 403:
        result['reason'] = 'quota_read_forbidden'
        result['next'] = ('Квоты неизвестны. В кабинете Cloud.ru откройте Администрирование → Квоты, '
                          'сверьте колонку остатка для проекта и его организации. Запишите проверку '
                          'и ставки из калькулятора через plan --preflight-file FILE.')
    save(state/'quota-report.json',result)
    return result


def owner_estimate(config, evidence):
    """The rates are contract-specific hourly totals (including VAT) for this exact selection."""
    for field in ('project_id', 'zone', 'flavor', 'disk_gb'):
        if config[field] != evidence.get(field):
            raise ValueError('Manual preflight does not match deployment: ' + field)
    for field in ('quota_source', 'remaining', 'price_source', 'checked_at'):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError('Manual preflight requires ' + field)
    rates = evidence.get('hourly_rub_including_vat', {})
    if not isinstance(rates, dict) or set(rates) != {'vm', 'disk', 'ipv4'}:
        raise ValueError('Supply hourly RUB totals for vm, disk and ipv4 including VAT')
    values = [finite(rate) for rate in rates.values()]
    if any(value is None for value in values):
        raise ValueError('Rates must be finite nonnegative numbers')
    hourly = sum(values)
    return {key: evidence[key] for key in ('project_id', 'zone', 'flavor', 'disk_gb', 'checked_at',
            'quota_source', 'remaining', 'price_source', 'hourly_rub_including_vat')} | {
        'base_hourly_rub': str(hourly), 'base_720h_rub': format(hourly * 720, '.2f'),
        's3_discrepancy': evidence.get('s3_discrepancy'),
        'variable_or_additional': ['S3 storage, requests and traffic', 'registry and HTTPS container usage'],
        'quota_evidence': 'owner observation; independent of API status'}


def review_key(state,phase):
    """Invalidate reviewed demands when target, plan or created resources change."""
    paths=['deployment.json','https-plan.json','https-journal.json'] if phase=='https' else ['deployment.json','plan-review.json','terraform/terraform.tfstate']
    values={}
    for name in paths:
        path=state/name
        if name=='https-journal.json' and not path.exists():
            plan=read(state/'https-plan.json') if (state/'https-plan.json').exists() else {}
            values[name]={'config':plan.get('config',{}),'completed':{},'resources':{}}
            continue
        if path.exists():
            if name=='https-journal.json':
                journal=read(path)
                values[name]={'config':journal['config'],'completed':journal['completed'],
                    'resources':{k:v.get('resource_id') for k,v in journal['intent'].items() if v.get('resource_id')}}
            else:values[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def review(state,requirements,phase):
    result=check(state,requirements)
    save(state/('quota-demands-'+phase+'.json'),{'key':review_key(state,phase),'requirements':requirements})
    return result


def preflight(state,phase):
    path=state/('quota-demands-'+phase+'.json');requirements=None
    if path.exists():
        value=read(path)
        if value.get('key')==review_key(state,phase):requirements=value['requirements']
    result=check(state,requirements)
    print('Quota preflight: '+result['status']+'; see quota-report.json. Physical capacity is not guaranteed.',file=sys.stderr,flush=True)
    if result['status']=='insufficient':
        raise ValueError('Insufficient quota for reviewed additional resources; preserve state and resume after quota/capacity is available')
    return result
