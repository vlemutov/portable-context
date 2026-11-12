"""Prepare a stateless Cloud.ru HTTPS edge and an authenticated VM origin.

No memory paths, cloud credentials, or database files belong in the edge image.
Run with the installer's private state directory; generated keys are not exports.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re


def hostname(value):
    value = value.lower()
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('Expected a DNS hostname without scheme, port, path or wildcard')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in value.split('.')):
        raise ValueError('Invalid DNS hostname')
    return value


def port(value):
    value = int(value)
    if not 1024 <= value <= 65535:
        raise ValueError('Use an unprivileged port between 1024 and 65535')
    return value


def _path(directory, name):
    return json.dumps((Path(directory) / name).as_posix())


def diagnostic_errors(layer, revision):
    """Fixed response markers only; never expose exception text or request data."""
    if revision is None:
        return ''
    return f'''handle_errors {{
        header X-PC-Failure-Layer {layer}
        header X-PC-{layer.title()}-Revision {rotation_revision(revision)}
        respond "Portable Context transport error" {{err.status_code}}
    }}'''


def edge_config(public_host, origin_ip, secrets='/tmp/pc-edge', listen_port=8080, origin_port=9443, revision=None):
    """The destination is fixed by the owner, never by an HTTP header or URL."""
    public_host = hostname(public_host)
    address = ipaddress.ip_address(origin_ip)
    address = '[' + str(address) + ']' if address.version == 6 else str(address)
    stamp = '' if revision is None else 'header >X-PC-Edge-Revision '+rotation_revision(revision)
    return f'''{{
    admin off
    auto_https off
    persist_config off
    log default {{
        output discard
    }}
}}
:{port(listen_port)} {{
    {stamp}
    {diagnostic_errors('edge', revision)}
    @health path /_pc/health
    handle @health {{
        respond "edge alive; not a memory readiness check" 200
    }}
    @public host {public_host}
    handle @public {{
        request_body {{
            max_size 16MB
        }}
        header X-Robots-Tag "noindex, nofollow"
        reverse_proxy https://{address}:{port(origin_port)} {{
            flush_interval -1
            header_down -Alt-Svc
            header_up Host {public_host}
            header_up X-Forwarded-Host {public_host}
            header_up X-Forwarded-Proto https
            header_up -X-Forwarded-For
            header_up -Forwarded
            header_up -X-Real-IP
            header_up -X-Forwarded-Port
            header_up -X-Forwarded-Prefix
            transport http {{
                tls_server_name {public_host}
                tls_trust_pool file {_path(secrets, 'origin-ca.pem')}
                tls_client_auth {_path(secrets, 'edge.pem')} {_path(secrets, 'edge-key.pem')}
                dial_timeout 10s
                response_header_timeout 540s
            }}
        }}
    }}
    respond 404
}}
'''


def rotation_revision(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('Invalid rotation revision')
    return value


def origin_config(public_host, secrets, listen_port=9443, application_port=8766, identity_port=None, revision=None):
    public_host = hostname(public_host)
    def proxy(target):
        return f'''reverse_proxy 127.0.0.1:{port(target)} {{
            flush_interval -1
            header_up Host {public_host}
            header_up X-Forwarded-Host {public_host}
            header_up X-Forwarded-Proto https
            header_up -X-Forwarded-For
            header_up -Forwarded
            header_up -X-Real-IP
            header_up -X-Forwarded-Port
            header_up -X-Forwarded-Prefix
            header_up -X-API-Key
        }}'''
    backend = proxy(application_port)
    transport = ''
    if identity_port is not None:
        if port(identity_port) == port(application_port):
            raise ValueError('Identity and MCP services must use distinct loopback ports')
        backend = f'''route {{
            @setup path /api/signup /api/signup/* /setup /setup/*
            respond @setup 404
            @mcp path /mcp /mcp/* /.well-known/oauth-protected-resource/mcp
            handle @mcp {{
                {proxy(application_port)}
            }}
            handle {{
                {proxy(identity_port)}
            }}
        }}'''
    if revision is not None:
        transport = f'''@transport path /_pc/transport
        handle @transport {{
            respond "{rotation_revision(revision)}" 200
        }}'''
        backend = 'handle {\n'+backend+'\n}'
    return f'''{{
    admin off
    auto_https off
    persist_config off
    log default {{
        output discard
    }}
}}
https://:{port(listen_port)} {{
    {'' if revision is None else 'header >X-PC-Origin-Revision '+rotation_revision(revision)}
    {diagnostic_errors('origin', revision)}
    tls {_path(secrets, 'origin.pem')} {_path(secrets, 'origin-key.pem')} {{
        client_auth {{
            mode require_and_verify
            trust_pool file {_path(secrets, 'edge-ca.pem')}
        }}
    }}
    @public host {public_host}
    handle @public {{
        {transport}
        request_body {{
            max_size 16MB
        }}
        {backend}
    }}
    respond 404
}}
'''


def issue_credentials(directory, public_host, *, days=90):
    """Separate trust roots: an edge client cannot impersonate the VM server.

    CA private keys are discarded. Renew by preparing a NEW generation and
    switching trust/client credentials with overlap, never overwrite in place.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    public_host = hostname(public_host)
    directory = Path(directory)
    if directory.exists():
        raise ValueError('Credential generation already exists; use a new directory for rotation')
    if not 1 <= days <= 90:
        raise ValueError('Credential lifetime must be 1–90 days')
    directory.mkdir(parents=True, mode=0o700)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days)

    def write(name, data):
        target = directory / name
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o600)

    for label, usage in [('origin', ExtendedKeyUsageOID.SERVER_AUTH), ('edge', ExtendedKeyUsageOID.CLIENT_AUTH)]:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Portable Context ' + label + ' CA')])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
              .not_valid_after(expires).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), True)
              .sign(ca_key, hashes.SHA256()))
        key = ec.generate_private_key(ec.SECP256R1())
        leaf_name = public_host if label == 'origin' else 'edge.portable-context.internal'
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, leaf_name)]))
                .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.ExtendedKeyUsage([usage]), True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, None, None), True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(leaf_name)]), False)
                .sign(ca_key, hashes.SHA256()))
        write(label + '-ca.pem', ca.public_bytes(serialization.Encoding.PEM))
        write(label + '.pem', leaf.public_bytes(serialization.Encoding.PEM))
        write(label + '-key.pem', key.private_bytes(serialization.Encoding.PEM,
              serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return {'expires_at': expires.isoformat(), 'renew_before': (expires-timedelta(days=30)).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--public-host', required=True)
    parser.add_argument('--origin-ip', required=True)
    args = parser.parse_args(argv)
    # Uses the existing cross-platform private directory / Windows ACL helper.
    from setup import private_directory, save
    state = private_directory(args.state)
    host = hostname(args.public_host)
    ip = str(ipaddress.ip_address(args.origin_ip))
    generation = state / 'gateway-generation'
    metadata = issue_credentials(generation, host)
    (generation / 'edge.Caddyfile').write_text(edge_config(host, ip), encoding='utf-8')
    (generation / 'origin.Caddyfile').write_text(origin_config(host, '/run/pc-origin'), encoding='utf-8')
    save(generation / 'manifest.json', dict(metadata, public_host=host, origin_ip=ip,
        memory_migration=False, status='prepared_not_deployed',
        edge_files=['origin-ca.pem', 'edge.pem', 'edge-key.pem', 'edge.Caddyfile'],
        origin_files=['edge-ca.pem', 'origin.pem', 'origin-key.pem', 'origin.Caddyfile']))
    print(json.dumps({'status': 'prepared_not_deployed', **metadata}))


if __name__ == '__main__':
    main()
