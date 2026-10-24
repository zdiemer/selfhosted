#!/usr/bin/env python3
"""Give each password-registration attempt a fresh identity after pod restarts."""
import sys
import yaml

REGISTER = 'cscli lapi register --machine "$USERNAME"'
PREFIX = '''# A pod name survives node reboot, but its registration credentials may not.
# Never delete or overwrite another live identity to recover registration.
set -eu
registration_id="$(cat /proc/sys/kernel/random/uuid)"
test -n "$registration_id"
export USERNAME="${USERNAME}-${registration_id}"
'''


def patch(documents):
    count = 0
    for doc in documents:
        if not doc or doc.get('kind') != 'DaemonSet':
            continue
        spec = doc.get('spec', {}).get('template', {}).get('spec', {})
        for container in spec.get('initContainers', []):
            if container.get('name') != 'wait-for-lapi-and-register':
                continue
            command = container.get('command', [])
            if len(command) != 3 or command[:2] != ['sh', '-c'] or REGISTER not in command[2]:
                raise ValueError('CrowdSec registration command changed; review post-renderer')
            if not command[2].startswith(PREFIX):
                command[2] = PREFIX + command[2]
            count += 1
    if count != 1:
        raise ValueError(f'Expected one password-registration DaemonSet, found {count}; review authentication mode')
    return documents


if __name__ == '__main__':
    try:
        yaml.safe_dump_all(patch([d for d in yaml.safe_load_all(sys.stdin) if d]), sys.stdout, sort_keys=False)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
