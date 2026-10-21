#!/usr/bin/env python3
"""Read-only proxy/firewall probes. Never display credentials or response bodies."""
import base64
import http.client
import json
from pathlib import Path
import subprocess

config = json.loads(Path('/etc/egress-iac-checks.json').read_text())
password = Path('/etc/squid/secret/cluster_password').read_text().strip()
credential = base64.b64encode((config['proxy_user'] + ':' + password).encode()).decode()


def proxy_status(target, authenticated=True, connect=False):
    conn = http.client.HTTPConnection('127.0.0.1', config['proxy_port'], timeout=15)
    headers = {'Proxy-Authorization': 'Basic ' + credential} if authenticated else {}
    try:
        conn.request('CONNECT' if connect else 'GET', target, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


assert proxy_status('http://example.com/', authenticated=False) == 407, 'Proxy authentication is not enforced'
assert proxy_status('example.com:443', connect=True) == 200, 'Authenticated public proxy connection failed'
for target in ('http://169.254.169.254/metadata/v1/id', 'http://127.0.0.1/',
               'http://[::1]/', 'http://100.121.136.39/',
               'http://' + config['public_ipv4'] + '/'):
    assert proxy_status(target) == 403, 'Squid did not reject a protected destination'
print('Proxy authentication and public CONNECT work; protected destinations return 403')

# Bypass Squid policy and test the kernel boundary under its actual worker UID.
probe = '''import socket
for address, port in [('169.254.169.254', 80), ('127.0.0.1', 2019), ('100.121.136.39', 30096)]:
    try:
        with socket.create_connection((address, port), timeout=3):
            raise SystemExit('FAIL: Squid worker can connect to a protected destination')
    except OSError:
        pass
'''
subprocess.run(['setpriv', '--reuid=proxy', '--regid=proxy', '--clear-groups',
                'python3', '-c', probe], check=True)
print('Kernel blocks Squid-worker metadata, local-admin and tailnet connections')
# Check the legitimate media path; do not wake the autopause Minecraft server.
media_probe = '''import socket,sys
for host in sys.argv[1:]:
    try:
        with socket.create_connection((host, 30096), timeout=3):
            sys.exit(0)
    except OSError:
        pass
sys.exit('No media backend reachable as Caddy')
'''
subprocess.run(['setpriv', '--reuid=caddy', '--regid=caddy', '--clear-groups',
                'python3', '-c', media_probe, *config['media_nodes']], check=True)
print('Caddy can reach a configured media backend')
