#!/usr/bin/env python3
"""Install the Steam-only runtime profiles through the existing node admin pod."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
pods = json.loads(subprocess.check_output(['kubectl', 'get', 'pods', '-n', 'infra', '-o', 'json']))
pod = next(p['metadata']['name'] for p in pods['items'] if p['spec'].get('nodeName') == 'zachd-ubuntu-6' and p['metadata']['name'].startswith('registry-node-config-') and p['status']['phase'] == 'Running')
files = {
    '/etc/apparmor.d/steam-headless': (root / 'profiles/steam-headless.apparmor').read_text(),
    '/var/lib/kubelet/seccomp/steam-headless.json': (root / 'profiles/steam-headless.json').read_text(),
}
code = '''import json, pathlib, subprocess, sys
for name, content in json.load(sys.stdin).items():
    path = pathlib.Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.new')
    temp.write_text(content)
    temp.chmod(0o644)
    temp.replace(path)
subprocess.run(['/usr/sbin/apparmor_parser', '-r', '/etc/apparmor.d/steam-headless'], check=True)
print('Steam node profiles installed')
'''
subprocess.run(['kubectl', 'exec', '-i', '-n', 'infra', pod, '--', 'nsenter', '-t', '1', '-m', '--', 'python3', '-c', code], input=json.dumps(files).encode(), check=True)
