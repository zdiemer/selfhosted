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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='write labels and persistent host sysctl')
    args = parser.parse_args()
    nodes = json.loads(subprocess.check_output(['kubectl', 'get', 'nodes', '-o', 'json']))['items']
    for node in nodes:
        name = node['metadata']['name']
        tier = 'low' if memory_bytes(node['status']['capacity']['memory']) < THRESHOLD else 'standard'
        print(f'{name}: {LABEL}={tier}; vm.max_map_count >= 262144', flush=True)
        if args.apply:
            with node_session(name) as execute:
                result = execute(['python3', '-c', HOST_CODE]).decode().strip()
                if result != 'host prerequisites verified':
                    raise RuntimeError(f'{name}: host verification failed')
            subprocess.run(['kubectl', 'label', 'node', name, f'{LABEL}={tier}', '--overwrite'], check=True)


if __name__ == '__main__':
    main()
