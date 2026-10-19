"""Small read-only exporter. Never returns message contents or credentials."""
import datetime
import json
import os
import ssl
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = '/var/run/secrets/kubernetes.io/serviceaccount'

def get(path):
    with open(SA + '/token') as f:
        token = f.read()
    req = urllib.request.Request('https://kubernetes.default.svc' + path,
                                 headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=SA + '/ca.crt'), timeout=10) as r:
        return json.load(r)['items']

def backup_metrics(schedules, backups, now):
    latest = {}
    for b in backups:
        for c in b.get('status', {}).get('conditions', []):
            if c['type'] == 'Completed' and c['status'] == 'True' and c.get('reason') == 'Succeeded':
                ts = datetime.datetime.fromisoformat(c['lastTransitionTime'].replace('Z', '+00:00')).timestamp()
                ns = b['metadata']['namespace']
                latest[ns] = max(latest.get(ns, 0), ts)
    lines = []
    for ns in sorted(set(json.loads(os.environ.get('BACKUP_NAMESPACES', '[]'))) | {s['metadata']['namespace'] for s in schedules if s.get('spec', {}).get('backup')}):
        # A configured schedule with no surviving success must fail closed.
        lines.append(f'home_backup_age_seconds{{namespace="{ns}"}} {now-latest.get(ns, 0)}')
    return lines

def collect():
    lines = []
    try:
        lines += backup_metrics(get('/apis/k8up.io/v1/schedules'), get('/apis/k8up.io/v1/backups'), time.time())
        lines.append('home_backup_collection_success 1')
    except Exception:
        lines.append('home_backup_collection_success 0')
    try:
        with urllib.request.urlopen('http://ntfy.infra.svc.cluster.local/v1/health', timeout=5) as r:
            ok = json.load(r).get('healthy') is True
    except Exception:
        ok = False
    lines.append(f'home_ntfy_healthy {int(ok)}')
    return '\n'.join(lines) + '\n'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404); return
        data = collect().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args): pass

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 9105), Handler).serve_forever()
