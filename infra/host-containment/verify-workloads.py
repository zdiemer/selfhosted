#!/usr/bin/env python3
"""Fail if running host namespace/privileged workloads exceed reviewed roles."""
import json
import subprocess

pods = json.loads(subprocess.check_output(['kubectl', 'get', 'pods', '-A', '-o', 'json']))['items']
privileged = {
    ('democratic-csi', 'democratic-csi-nfs-node-sa'): {'csi-driver'},
    ('democratic-csi', 'democratic-csi-iscsi-node-sa'): {'csi-driver'},
    ('kube-system', 'csi-smb-controller-sa'): {'smb'},
    ('kube-system', 'csi-smb-node-sa'): {'smb'},
    ('kubevirt', 'kubevirt-handler'): {'virt-handler', 'virt-launcher'},
}
exporter = ('infra', 'node-exporter-prometheus-node-exporter')
handler = ('kubevirt', 'kubevirt-handler')
host_network = (set(privileged) - {handler}) | {exporter}
counts = {}
for pod in pods:
    if pod['status']['phase'] in ('Succeeded', 'Failed') or pod['metadata'].get('deletionTimestamp'):
        continue
    spec = pod['spec']
    key = (pod['metadata']['namespace'], spec.get('serviceAccountName', 'default'))
    name = pod['metadata']['name']
    assert not spec.get('hostIPC'), name + ': host IPC enabled'
    assert not spec.get('hostPID') or key == handler, name + ': unreviewed host PID'
    assert not spec.get('hostNetwork') or key in host_network, name + ': unreviewed host network'
    containers = spec['containers'] + spec.get('initContainers', []) + spec.get('ephemeralContainers', [])
    for c in containers:
        sc = c.get('securityContext', {})
        if sc.get('privileged'):
            assert c['name'] in privileged.get(key, set()), name + ': unreviewed privileged container'
        if key in host_network and not sc.get('privileged'):
            assert sc.get('allowPrivilegeEscalation') is False, name + ': privilege escalation allowed'
            assert sc.get('capabilities', {}).get('drop') == ['ALL'], name + ': capabilities not dropped'
            profile = sc.get('seccompProfile', spec.get('securityContext', {}).get('seccompProfile', {}))
            assert profile.get('type') == 'RuntimeDefault', name + ': seccomp missing'
    for v in spec.get('volumes', []):
        if v.get('hostPath', {}).get('path') == '/':
            assert key in (exporter, ('democratic-csi', 'democratic-csi-iscsi-node-sa')), name + ': unexpected whole-host-root mount'
            assert key != exporter or all(m.get('readOnly') for c in containers for m in c.get('volumeMounts', [])
                       if m['name'] == v['name']), name + ': writable host root'
    if key in host_network or key == handler:
        counts[key] = counts.get(key, 0) + 1
for key, count in sorted(counts.items()):
    print('/'.join(key), count, 'reviewed pods')
print('No unreviewed privileged containers, host namespaces or whole-root mounts')
