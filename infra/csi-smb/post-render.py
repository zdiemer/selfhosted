#!/usr/bin/env python3
"""Constrain non-mount sidecars and make SMB controller metrics loopback-only."""
import sys
import yaml

docs = list(yaml.safe_load_all(sys.stdin))
patched = set()
for doc in docs:
    if not doc or doc.get('kind') not in ('Deployment', 'DaemonSet'):
        continue
    spec = doc['spec']['template']['spec']
    for c in spec['containers']:
        if c['name'] == 'smb':
            if doc['kind'] == 'Deployment':
                old = '--metrics-address=0.0.0.0:29644'
                if old not in c['args']:
                    sys.exit('Upstream SMB metrics argument changed; review before deploying')
                c['args'][c['args'].index(old)] = '--metrics-address=127.0.0.1:29644'
            continue
        c.setdefault('securityContext', {}).update(
            allowPrivilegeEscalation=False, capabilities={'drop': ['ALL']},
            readOnlyRootFilesystem=True, seccompProfile={'type': 'RuntimeDefault'})
    patched.add(doc['metadata']['name'])
if patched != {'csi-smb-controller', 'csi-smb-node'}:
    sys.exit(f'Unexpected SMB workloads: {patched}')
yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)
