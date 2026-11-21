"""VM catalog evidence and reviewed creation rejections, not capacity guarantees."""

VM_REJECTIONS = {(409, 'no_free_node'), (404, 'preset_not_found'),
                 (400, 'servers_is_run_out_error')}


def is_vm_rejection(status, code):
    return (status, code) in VM_REJECTIONS


def zones_for_preset(row, locations):
    zones = sorted({z for loc in locations.get('locations', [])
                    if loc.get('location') == row.get('location')
                    for z in loc.get('availability_zones', []) if isinstance(z, str)})
    # Some API/provider versions expose a preset-specific zone; never broaden it.
    if row.get('availability_zone'):
        zones = [z for z in zones if z == row['availability_zone']]
    return zones


def catalog_evidence(zone):
    return {'capacity': 'unknown', 'panel_visibility': 'unknown',
            'pilot_zone': zone == 'msk-1',
            'note': 'Catalog membership does not establish orderability, account eligibility or free capacity. '
                    'Pilot zones are historical evidence only. If absent in the panel, review another zone before creating.'}


def rejection_guidance(journal):
    server = journal.get('resources', {}).get('server', {})
    rejection = server.get('creation_rejection', {})
    if server.get('id') or not is_vm_rejection(rejection.get('status'), rejection.get('error_code')):
        return None
    config = journal.get('config', {})
    return {'category': 'vm_creation_rejected', 'http_status': rejection['status'],
            'provider_error_code': rejection['error_code'], 'response_id': rejection.get('response_id'),
            'preset_id': config.get('server_preset_id'), 'availability_zone': config.get('availability_zone'),
            'submitted_at': server.get('submitted_at'), 'cause': 'not_confirmed',
            'next': 'Do not repeat apply blindly. Run timeweb-vm-options, review region/tier/price, '
                    'then timeweb-select-vm and approve the new plan in the SAME state. '
                    'Selection checks the complete VM inventory first. No automatic deletion. '
                    'This code alone proves neither an account quota nor a region-wide outage.'}
