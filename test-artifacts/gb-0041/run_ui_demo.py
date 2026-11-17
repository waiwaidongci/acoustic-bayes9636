"""Serve the real backend + built frontend for browser verification.

Redis is a fakeredis TCP server (no docker in this sandbox); MySQL is SQLite.
The browser only talks to the app through the same `/api` paths nginx would
proxy, so the HTTP surface under test is real.
"""
import json, os, sys, tempfile, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(tempfile.mkdtemp(prefix='ui-demo-'))
os.environ.update(
    DATABASE_URL='sqlite:///' + str(WORK / 'demo.db'),
    SEED_DEMO='true',
    JWT_SECRET='ui-demo-secret',
    ARTIFACT_DIR=str(WORK / 'artifacts'),
    MIGRATION_BATCH_SIZE='25',
    REDIS_URL='redis://127.0.0.1:6398/0',
)
sys.path.insert(0, str(ROOT / 'backend'))

import fakeredis
server = fakeredis.TcpFakeServer(('127.0.0.1', 6398), server_type='redis')
threading.Thread(target=server.serve_forever, daemon=True).start()

import uvicorn
from src.main import app
from src.worker import main as worker_loop
threading.Thread(target=worker_loop, daemon=True).start()

class ApiHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT / 'frontend'), **k)

    def translate_path(self, path):
        if path.startswith('/api/'):
            path = path[4:]
            return path
        return super().translate_path(path)

    def do_GET(self):
        if self.path.startswith('/api/'):
            self.path = self.path[4:]
            self.proxy()
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/'):
            self.path = self.path[4:]
            self.proxy()
            return
        self.send_error(404)

    def do_PUT(self):
        if self.path.startswith('/api/'):
            self.path = self.path[4:]
            self.proxy()
            return
        self.send_error(404)

    def proxy(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(length) if length else None
        from urllib.request import Request
        from urllib.error import HTTPError
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'content-length')}
        request = Request('http://127.0.0.1:8000' + self.path, data=body, headers=headers, method=self.command)
        try:
            with urlopen(request) as response:
                data = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in ('content-length', 'transfer-encoding', 'connection'):
                        continue
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except HTTPError as error:
            data = error.read()
            self.send_response(error.code)
            self.send_header('Content-Type', error.headers.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            payload = json.dumps({'detail': str(exc)}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *a):
        pass

config = uvicorn.Config(app, host='127.0.0.1', port=8000, log_level='warning')
api = uvicorn.Server(config)
threading.Thread(target=api.run, daemon=True).start()
for _ in range(100):
    try:
        urlopen('http://127.0.0.1:8000/health'); break
    except Exception:
        time.sleep(0.2)
httpd = ThreadingHTTPServer(('127.0.0.1', 18245), ApiHandler)
print('READY', WORK, flush=True)
httpd.serve_forever()
