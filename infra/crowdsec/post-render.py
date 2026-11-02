#!/usr/bin/env python3
"""Apply tracked notification policy and restart-safe agent registration."""
import hashlib
import json
from pathlib import Path
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


def patch_profiles(documents, scenarios):
    """Override the vault's routine list, preserving decisions and credentials."""
    checksums = {}
    for doc in documents:
        if not doc or doc.get('kind') != 'ConfigMap' or 'profiles.yaml' not in doc.get('data', {}):
            continue
        profiles = list(yaml.safe_load_all(doc['data']['profiles.yaml']))
        routine = [p for p in profiles if p and p.get('name') == 'routine_web_scanners']
        if not routine:
            if any(p and p.get('notifications') for p in profiles):
                raise ValueError('Notifying CrowdSec profiles lack routine_web_scanners; review policy')
            continue  # Fresh install without notification credentials.
        if len(routine) != 1 or profiles[0] is not routine[0]:
            raise ValueError('Expected one routine scanner profile before notifying profiles')
        profile = routine[0]
        if profile.get('notifications') or profile.get('on_success') != 'break':
            raise ValueError('Routine scanner profile must stop before notification')
        profile['filters'] = ['Alert.Remediation == true && Alert.GetScope() == "Ip" '
                              '&& Alert.GetScenario() in ' + json.dumps(scenarios)]
        content = yaml.safe_dump_all(profiles, sort_keys=False)
        doc['data']['profiles.yaml'] = content
        checksums[doc['metadata']['name']] = hashlib.sha256(content.encode()).hexdigest()
    # The chart's checksum predates post-rendering; roll only profile consumers.
    for doc in documents:
        if not doc or doc.get('kind') not in ('Deployment', 'DaemonSet'):
            continue
        template = doc['spec']['template']
        for volume in template['spec'].get('volumes', []):
            name = volume.get('configMap', {}).get('name')
            if name in checksums:
                template.setdefault('metadata', {}).setdefault('annotations', {})['checksum/notification-profiles'] = checksums[name]
    return documents


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
        values = yaml.safe_load((Path(__file__).parent / 'values.yaml').read_text())
        documents = patch([d for d in yaml.safe_load_all(sys.stdin) if d])
        yaml.safe_dump_all(patch_profiles(documents, values['profiles']['routineScenarios']), sys.stdout, sort_keys=False)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
