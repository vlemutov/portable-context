"""Guide a single existing Cloud.ru VM through SSH recovery and a reviewed /32 change."""
import ipaddress
import platform
import time

import network
from setup import configure, plan, read, ssh_args, ssh_read


def cloud_source(state):
    snapshot = read(state / 'terraform/terraform.tfstate')
    values = [row['attributes'].get('remote_ip_prefix') for item in snapshot.get('resources', [])
              if item.get('mode') == 'managed' and item.get('type') == 'cloudru_evolution_compute_security_group_rule'
              and item.get('name') == 'ssh' and not item.get('module') for row in item['instances']
              if not row.get('deposed')]
    return values[0] if len(values) == 1 else None


def recover(state, args):
    config = read(state / 'deployment.json')
    if config['mode'] != 'cloudru' or not config.get('host') or not (state/'terraform/terraform.tfstate').exists():
        raise ValueError('SSH recovery requires the private state of the existing Cloud.ru VM')
    if args.plan_route:
        return network.make_plan(state, 'server', args.interface, args.gateway, platform.system() == 'Linux')
    if args.plan_source:
        measurement = read(state/'ssh-egress.json')
        if (measurement['config'] != network.digest(config) or measurement['vm'] != config['host']
                or not 0 <= time.time() - measurement['measured_at'] <= 600):
            raise ValueError('Network or deployment changed; repeat ssh-egress --save-for-plan')
        cidr = ipaddress.IPv4Network(measurement['suggested_ssh_cidr'])
        if cidr.prefixlen != 32 or not cidr.network_address.is_global or cidr.network_address.is_multicast:
            raise ValueError('A measured public IPv4 /32 is required')
        old = cloud_source(state)
        configure(state, {'ssh_cidr': str(cidr)})
        if old == str(cidr):
            return {'source': old, 'next': 'SSH source already matches the Terraform state. Run check-ssh.'}
        return {**plan(state, ssh_only=True), 'previous_source_in_state': old, 'measured_source': str(cidr),
                'interface': measurement['interface'], 'gateway': measurement['gateway']}
    report = {'host': config['host'], 'configured_source': config['ssh_cidr'],
              'source_in_terraform_state': cloud_source(state)}
    try:
        result = ssh_read(ssh_args(state, config), 'true', label='SSH login', attempts=1)
    except ValueError as exc:
        if 'SSH transport unavailable' not in str(exc):
            raise
        details = network.inspect(state, 'server')
        return {**report, 'ssh': 'transport unavailable', 'endpoints': details['endpoints'],
                'gateways': details['candidates'], 'route': details['linux_policy'].get('effective_routes', []),
                'next': 'Check PC-BOOT in the VM console. If all four fields are ok, review ssh-recover --plan-route. '
                        'When the direct route is active, measure SSH egress before changing the cloud rule.'}
    if result.returncode:
        raise ValueError('Authenticated SSH command failed')
    return {**report, 'ssh': 'authenticated', 'next': 'Resume install --https in this state. '
            'Keep any temporary server route until owner enrollment and required SSH operations finish.'}
