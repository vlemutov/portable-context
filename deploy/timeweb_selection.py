"""Reviewable alternatives for a rejected VM creation; never replace a running VM."""
import copy
from pathlib import Path
from setup import read,save
from timeweb_api import API,integer
from timeweb_setup import catalog,find_row,plan,digest,target
from timeweb_vm import is_vm_rejection,zones_for_preset,catalog_evidence,rejection_guidance,require_selection_idle


def family(row):
    tags=row.get('tags',[])
    if 'ultra_msk_nvme' in tags:return 'ultra'
    if any('dedicated' in v for v in tags):return 'dedicated_cpu'
    if any('high_cpu' in v or 'highcpu' in v for v in tags):return 'high_cpu'
    if row.get('disk_type')=='nvme' and any(v in tags for v in ('msk_nvme','discount35')):return 'premium'
    return 'other'


def options(state,locations=None,max_price=None,*,data=None):
    config=read(state/'deployment.json');data=catalog(state) if data is None else data
    selected=find_row(data['servers'],config['server_preset_id'])
    if selected is None:raise ValueError('Current VM preset missing; review current catalog manually')
    locations=locations or [selected['location']]
    if any(not v.startswith('ru-') for v in locations):raise ValueError('Only Russian fallback locations are supported')
    ceiling=float(selected['price'] if max_price is None else max_price)
    if not 0<ceiling<float('inf'):raise ValueError('Price ceiling must be positive and finite')
    journal=read(state/'timeweb-journal.json') if (state/'timeweb-journal.json').exists() else {}
    rejected={(v['rejected_selection']['preset_id'],v['rejected_selection']['availability_zone'])
              for v in journal.get('vm_selection_history',[]) if v.get('rejected_selection')}
    current=rejection_guidance(journal)
    if current:rejected.add((current['preset_id'],current['availability_zone']))
    rows=[];excluded=[]
    for row in data['servers']['server_presets']:
        if row.get('location') not in locations or float(row['price'])>ceiling:continue
        if any(row.get(k,0)<max(selected.get(k,0),minimum) for k,minimum in [('cpu',1),('ram',2048),('disk',20480)]):continue
        if row.get('disk_type')!=selected.get('disk_type'):continue
        all_zones=zones_for_preset(row,data['locations'])
        zones=[z for z in all_zones if (row['id'],z) not in rejected]
        excluded.extend({'preset_id':row['id'],'availability_zone':z,'reason':'previous_creation_rejected'}
                        for z in all_zones if z not in zones)
        if not zones:continue
        rows.append({**{k:row.get(k) for k in ('id','location','price','cpu','ram','disk','disk_type','cpu_frequency','bandwidth','tags','description_short')},
            'family':family(row),'availability_zones':zones,'capacity':'unknown',
            'zone_evidence':{z:catalog_evidence(z) for z in zones},'monthly_price_delta':row['price']-selected['price']})
    return {'current_preset_id':selected['id'],'max_monthly_vm_price':ceiling,
        'options':sorted(rows,key=lambda v:(v['price'],v['id'])),'excluded':excluded,
        'note':'Catalog presence does not prove orderability or capacity. Empty options do not prove an account limit; review other Russian locations and price ceiling. No resources created.'}


def _finish(state,record,approval):
    if approval!=record['preview']['plan_sha256']:raise ValueError('VM selection approval does not match')
    for name in ('deployment.json','timeweb-journal.json','timeweb-plan.json'):
        value=read(state/name)
        if value not in (record['before'][name],record['after'][name]):
            raise ValueError('Installation changed during VM selection; preserve state and review')
    # Durable intent lets this exact sequence resume after interruption at any write.
    for name,value in record['after'].items():save(state/name,value)
    record['complete']=True;save(state/'timeweb-vm-selection.json',record)
    return {'selected':record['preview']['selected'],'next':'Review the new plan and apply --approve-plan '+record['after']['timeweb-plan.json']['plan_sha256']}


def select(state,preset_id,zone,approval=None):
    from portable_context.restic_backup import file_lock
    state=Path(state);preset_id=integer(preset_id)
    with file_lock(state/'timeweb-install.lock'):
        require_selection_idle(state)
        transition=state/'timeweb-vm-selection.json'
        if transition.exists():
            pending=read(transition)
            if not pending.get('complete'):
                if pending['preview']['selected']!={'preset_id':preset_id,'availability_zone':zone}:
                    raise ValueError('Finish interrupted VM selection first')
                if approval is None:return pending['preview']
                return _finish(state,pending,approval)
        config=read(state/'deployment.json');j=read(state/'timeweb-journal.json')
        resources=j.get('resources',{});server=resources.get('server',{})
        error=j.get('last_error',{})
        if config.get('host') or server.get('id') or j.get('server_ipv4') or set(resources)-{'project','ssh_key','server'}:
            raise ValueError('VM already exists or installation advanced; automatic replacement is forbidden')
        rejection=server.get('creation_rejection')
        if rejection is None and error.get('stage')=='infrastructure' and error.get('type')=='TimewebError':
            # Compatibility with early journals; newer journals retain per-resource evidence.
            rejection={'status':error.get('http_status'),'error_code':error.get('provider_error_code')}
        if server.get('submitted') or not rejection or not is_vm_rejection(rejection.get('status'),rejection.get('error_code')):
            raise ValueError('Only a definitive VM creation rejection permits fallback; reconcile ambiguous outcomes first')
        inventory=API(state).request('/api/v1/servers')
        servers=inventory['servers']
        if (not isinstance(servers,list) or any(type(count) is not int or count!=len(servers)
            for key,count in inventory.get('meta',{}).items() if key in ('total','amount'))):
            raise ValueError('Incomplete VM inventory; reconcile manually before fallback')
        if any(v.get('name')==config['name'] for v in servers):
            raise ValueError('Matching VM exists; reconcile it before considering any fallback')
        oldplan=read(state/'timeweb-plan.json')
        selected={**config,'server_preset_id':preset_id,'availability_zone':zone}
        candidate=plan(state,config=selected,persist=False)
        old=oldplan['presets']['servers'];new=candidate['presets']['servers']
        if any(new.get(k,0)<old.get(k,0) for k in ('cpu','ram','disk')) or new.get('disk_type')!=old.get('disk_type'):
            raise ValueError('Fallback must preserve minimum CPU, RAM, disk capacity and disk type')
        preview={'selected':{'preset_id':preset_id,'availability_zone':zone},'old':old,'new':new,
            'rejection':rejection,'availability':catalog_evidence(zone),
            'retry_same_selection':preset_id==config['server_preset_id'] and zone==config['availability_zone'],
            'family':family(new),'monthly_vm_price_delta':new['price']-old['price'],
            'capacity':'unknown','old_state_sha256':digest({'config':config,'journal':j,'plan':oldplan}),
            'new_plan_sha256':candidate['plan_sha256'],
            'effect':'Change only the uncreated VM selection; preserve project and SSH key. No API mutations. Recurring price requires review.'}
        preview['plan_sha256']=digest(preview)
        if approval is None:return preview
        if approval!=preview['plan_sha256']:raise ValueError('VM fallback preview changed; review fresh prices and state')
        updated=copy.deepcopy(j)
        updated.setdefault('vm_selection_history',[]).append({'previous_server':server,'rejection':rejection,'selection':preview,
            'rejected_selection':{'preset_id':config['server_preset_id'],'availability_zone':config['availability_zone']}})
        updated['config']=target(selected);updated['resources'].pop('server',None);updated.pop('last_error',None)
        record={'preview':preview,'complete':False,
            'before':{'deployment.json':config,'timeweb-journal.json':j,'timeweb-plan.json':oldplan},
            'after':{'deployment.json':selected,'timeweb-journal.json':updated,'timeweb-plan.json':candidate}}
        save(transition,record)
        return _finish(state,record,approval)
