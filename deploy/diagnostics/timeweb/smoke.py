"""Full synthetic OAuth/transport regression on Linux, before public deployment."""
from datetime import datetime, timedelta, timezone
import json
import http.cookiejar
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import tempfile
import threading
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import server
from probe import Probe, NoRedirect

server.KEY = secrets.token_urlsafe(48)
hostname = 'synthetic.example.invalid'
original_resolve = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **kw: original_resolve('127.0.0.1' if host == hostname else host, *a, **kw)
temporary = tempfile.TemporaryDirectory(prefix='synthetic-tls-')
root = Path(temporary.name)
key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
    .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
    .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
    .sign(key, hashes.SHA256()))
certificate = cert.public_bytes(serialization.Encoding.PEM)
(root / 'cert.pem').write_bytes(certificate)
(root / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(root / 'cert.pem', root / 'key.pem')
app = server.ThreadingHTTPServer(('127.0.0.1', 443), server.Handler)
app.socket = context.wrap_socket(app.socket, server_side=True)
worker = threading.Thread(target=app.serve_forever, daemon=True)
worker.start()
probe = Probe('https://' + hostname, server.KEY)
# Trust the generated local test certificate. Public cloud probes use ordinary
# system trust, never this context. Hostname validation stays enabled in both.
probe.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    urllib.request.HTTPSHandler(context=ssl.create_default_context(cadata=certificate.decode())))
try:
    probe.run(long_stream_seconds=4)
    print('Full synthetic Pocket ID OAuth and transport regression passed')
except Exception:
    print(json.dumps(probe.results))
    raise
finally:
    app.shutdown()
    app.server_close()
    worker.join()
    process = server.STATE.get('process')
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    directory = server.STATE.get('directory')
    if directory:
        shutil.rmtree(directory)
    temporary.cleanup()
