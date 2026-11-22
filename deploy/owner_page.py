"""Short-lived loopback handoff for the recorded installation. Never log requests or codes."""
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import time
import webbrowser

from https_setup import connection_receipt, owner_login
from setup import read, save


def render(state, origin, handoff, nonce):
    path = Path(handoff['private_file']).resolve()
    if path.parent != state.resolve() or not path.match('owner-login-*.json'):
        raise ValueError('Invalid private owner handoff')
    data = read(path)
    url = origin + '/login/alternative/code?redirect=/settings/account'
    if data['login_url'] != url or not origin.startswith('https://'):
        raise ValueError('Owner login URL does not match this installation')
    expires = path.stat().st_mtime + 60 * data['expires_in_minutes']
    active = time.time() < expires
    when = datetime.fromtimestamp(expires).astimezone().strftime('%H:%M %Z') if active else ''
    entry = (f'<p>Код до {escape(when)}: <code id="code">{escape(data["code"])}</code></p>'
             '<button id="copy">Скопировать код</button> '
             f'<a href="{escape(url, quote=True)}" rel="noreferrer" target="_blank">Открыть вход в эту память</a>'
             if active else '<p>Время действия кода истекло.</p>')
    return (f'<!doctype html><meta charset="utf-8"><title>Первый вход в память</title>'
            f'<h1>Первый вход в память</h1><p>{escape(origin)}</p>{entry}'
            '<p>Введите код на странице входа и добавьте passkey в настройках аккаунта. '
            'Затем подключите своего клиента и подтвердите его права.</p>'
            '<form method="post"><button>Получить новый код</button></form>'
            f'<script nonce="{nonce}">document.getElementById("copy")?.addEventListener("click", '
            'async e=>{await navigator.clipboard.writeText(document.getElementById("code").textContent);'
            'e.target.textContent="Скопировано"})</script>').encode()


def open_page(state):
    origin = connection_receipt(state)['origin']
    pointer = state / 'owner-enrollment.json'
    if not pointer.is_file():
        save(pointer, owner_login(state))
    handoff = read(pointer)
    route = '/' + secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def serve_page(self, renew=False):
            nonlocal handoff
            if self.path != route or self.headers.get('Host') != authority:
                self.send_error(404)
                return
            if renew and self.headers.get('Origin') != 'http://' + authority:
                self.send_error(403)
                return
            try:
                if renew:
                    handoff = owner_login(state)
                    save(pointer, handoff)
                    self.send_response(303)
                    self.send_header('Location', route)
                    self.end_headers()
                    return
                body = render(state, origin, handoff, nonce)
            except Exception:
                self.send_error(503, 'Owner handoff unavailable. Check SSH and the private state.')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{nonce}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        do_GET = serve_page

        def do_POST(self):
            self.serve_page(renew=True)

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        authority = f'127.0.0.1:{server.server_port}'
        url = 'http://' + authority + route
        if not webbrowser.open(url):
            raise ValueError('Start owner-page from a local graphical session with a browser')
        print('Owner page opened locally. Ctrl+C closes it. Renewing the code requires SSH.', flush=True)
        server.timeout = 1
        deadline = time.monotonic() + 30 * 60
        try:
            while time.monotonic() < deadline:
                server.handle_request()
        except KeyboardInterrupt:
            pass
    return {'owner_page': 'closed'}
