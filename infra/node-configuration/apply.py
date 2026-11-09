#!/usr/bin/env python3
"""Apply shared node prerequisites using the operator's existing admin identity."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/lib'))
from node_exec import node_session

LABEL = 'infra.zachd.dev/memory-tier'
THRESHOLD = 6 * 1024**3


def memory_bytes(value):
    for suffix, multiplier in [('Ki', 1024), ('Mi', 1024**2), ('Gi', 1024**3)]:
        if value.endswith(suffix):
            return int(value[:-len(suffix)]) * multiplier
    return int(value)


HOST_CODE = '''
from pathlib import Path
import subprocess
current = int(subprocess.check_output(['sysctl', '-n', 'vm.max_map_count']))
# Never reduce a larger host setting required by another application.
target = max(262144, current)
path = Path('/etc/sysctl.d/70-shared-search.conf')
content = '# Managed by selfhosted/infra/node-configuration\\nvm.max_map_count = %d\\n' % target
if not path.exists() or path.read_text() != content:
    path.write_text(content)
    path.chmod(0o644)
subprocess.run(['sysctl', '-w', 'vm.max_map_count=%d' % target], check=True, stdout=subprocess.DEVNULL)
assert int(subprocess.check_output(['sysctl', '-n', 'vm.max_map_count'])) >= 262144
print('host prerequisites verified')
'''

# Nodes must never resolve through the tailnet's DNS. A cluster-hosted resolver
# (infra/adguard) set as Tailscale's global nameserver would otherwise become
# the nodes' own resolver: containerd can't pull the image that resolver needs,
# and k3s-resolv-conf.py hands 100.100.100.100 to CoreDNS as a pod upstream. On
# 2026-11-08 that loop black-holed DNS for the whole tailnet. `tailscale set`
# persists in tailscaled's state, so a reboot keeps it.
TAILSCALE_CODE = '''
import json, shutil, subprocess
if not shutil.which('tailscale'):
    print('tailscale dns: not installed')
    raise SystemExit
subprocess.run(['tailscale', 'set', '--accept-dns=false'], check=True)
prefs = json.loads(subprocess.check_output(['tailscale', 'debug', 'prefs']))
assert prefs.get('CorpDNS') is False, prefs.get('CorpDNS')
print('tailscale dns: off')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='write labels and persistent host sysctl')
    args = parser.parse_args()
    nodes = json.loads(subprocess.check_output(['kubectl', 'get', 'nodes', '-o', 'json']))['items']
    for node in nodes:
        name = node['metadata']['name']
        tier = 'low' if memory_bytes(node['status']['capacity']['memory']) < THRESHOLD else 'standard'
        print(f'{name}: {LABEL}={tier}; vm.max_map_count >= 262144; tailscale accept-dns=false', flush=True)
        if args.apply:
            with node_session(name) as execute:
                result = execute(['python3', '-c', HOST_CODE]).decode().strip()
                if result != 'host prerequisites verified':
                    raise RuntimeError(f'{name}: host verification failed')
                result = execute(['python3', '-c', TAILSCALE_CODE]).decode().strip()
                if not result.startswith('tailscale dns: '):
                    raise RuntimeError(f'{name}: tailscale DNS verification failed')
                print(f'  {result}', flush=True)
            subprocess.run(['kubectl', 'label', 'node', name, f'{LABEL}={tier}', '--overwrite'], check=True)


if __name__ == '__main__':
    main()
