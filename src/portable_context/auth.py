"""Minimal bearer protection for a single owner's MCP endpoint (not an OAuth issuer)."""
import hmac


class BearerAuth:
    def __init__(self, app, token):
        if not token or len(token) < 24:
            raise ValueError('PC_MCP_TOKEN must contain at least 24 characters')
        self.app = app
        self.expected = ('Bearer ' + token).encode('utf-8')

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            supplied = dict(scope.get('headers', [])).get(b'authorization', b'')
            if not hmac.compare_digest(supplied, self.expected):
                body = b'{"error":"Unauthorized"}'
                await send({'type': 'http.response.start', 'status': 401,
                            'headers': [(b'content-type', b'application/json'),
                                        (b'www-authenticate', b'Bearer'),
                                        (b'content-length', str(len(body)).encode())]})
                await send({'type': 'http.response.body', 'body': body})
                return
        await self.app(scope, receive, send)
