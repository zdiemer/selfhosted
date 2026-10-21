#!/usr/bin/env python3
"""Read-only runtime checks; never print container environments or metadata."""
import json
import subprocess
import socket

public_ip = socket.gethostbyname("store.steampowered.com")

for name in ('whatnowgg-app-1', 'whatnowgg-caddy-1'):
    spec = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    limits = spec['HostConfig']
    assert limits['Memory'] > 0, f'{name}: no memory cap'
    assert limits['NanoCpus'] > 0, f'{name}: no CPU cap'
    assert limits['PidsLimit'] > 0, f'{name}: no process cap'
    assert limits['ReadonlyRootfs'], f'{name}: writable root'
    assert 'ALL' in limits['CapDrop'], f'{name}: broad capabilities'
    assert 'no-new-privileges:true' in limits['SecurityOpt'], f'{name}: privilege escalation allowed'
    assert not spec['State']['OOMKilled'], f'{name}: OOM killed'
    # Host Python in the container's network namespace probes the exact same
    # network boundary even for Caddy, whose image has no Python installation.
    script = '''import socket, ssl
for target, port in [('169.254.169.254', 80), ('10.116.0.2', 22)]:
    try:
        with socket.create_connection((target, port), timeout=3):
            raise SystemExit('FAIL: protected destination is reachable')
    except OSError:
        pass
with socket.create_connection((__PUBLIC_IP__, 443), timeout=10) as sock:
    with ssl.create_default_context().wrap_socket(sock, server_hostname='store.steampowered.com'):
        pass
'''.replace('__PUBLIC_IP__', repr(public_ip))
    subprocess.run(['nsenter', '--target', str(spec['State']['Pid']), '--net',
                    'python3', '-c', script], check=True)
    print(f'{name}: limits active; metadata and host SSH blocked; public HTTPS works')
subprocess.run(['docker', 'exec', 'whatnowgg-caddy-1', 'wget', '-q', '-T', '5',
                '-O', '/dev/null', 'http://app:8080/healthz'], check=True)
print('Caddy can reach the application health endpoint')
