"""Synthetic origin for gateway acceptance. No memory imports or file access."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


class Probe(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/probe/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            for i in range(3):
                self.wfile.write(('data: ' + str(i) + '\n\n').encode())
                self.wfile.flush()
                time.sleep(1)
            return
        if self.path != '/probe':
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'service': 'portable-context-gateway-probe', 'memory_connected': False,
                                    'host': self.headers.get('Host'),
                                    'scheme': self.headers.get('X-Forwarded-Proto')}).encode())

    def do_POST(self):
        if self.path != '/probe/echo':
            self.send_response(404)
            self.end_headers()
            return
        size = int(self.headers.get('Content-Length', '0'))
        if size > 2 * 1024 * 1024:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(size)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18766), Probe).serve_forever()
