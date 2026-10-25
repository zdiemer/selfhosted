"""Read local arr health with its on-disk API key; expose counts and bounded health-check identifiers."""
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def collect():
    app = os.environ['ARR_APP']
    key = ET.parse('/config/config.xml').getroot().findtext('ApiKey')
    if not key:
        raise ValueError('API key unavailable')
    version = 'v1' if app == 'prowlarr' else 'v3'
    req = urllib.request.Request(f'http://127.0.0.1:{os.environ["ARR_PORT"]}/api/{version}/health', headers={'X-Api-Key':key})
    with urllib.request.urlopen(req, timeout=5) as response:
        checks = json.load(response)
    if not isinstance(checks, list):
        raise ValueError('Unexpected health response')
    rows = []
    for severity in ['warning', 'error']:
        issues = [c for c in checks if c.get('type', '').lower() == severity]
        # Only class identifiers, never messages, URLs or user configuration.
        names = sorted({re.sub(r'[^A-Za-z0-9_]', '', str(c.get('source', 'Unknown')))[:80] or 'Unknown' for c in issues})
        identifiers = ','.join(names[:8])
        rows.append(f'home_arr_health_issues{{app="{app}",severity="{severity}",checks="{identifiers}"}} {len(issues)}')
    return '\n'.join(rows) + '\n'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404); return
        try:
            data = collect().encode()
        except Exception:
            self.send_error(503); return
        self.send_response(200)
        self.send_header('Content-Type','text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args): pass

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0',9105),Handler).serve_forever()
