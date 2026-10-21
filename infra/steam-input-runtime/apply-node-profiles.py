#!/usr/bin/env python3
"""Install the Steam-only runtime profiles through the existing node admin pod."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
import sys
sys.path.insert(0,str(root.parents[1] / 'scripts/lib'))
from node_exec import node_session
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
with node_session('zachd-ubuntu-6') as execute:
    print(execute(['python3','-c',code],json.dumps(files).encode()).decode().strip())
