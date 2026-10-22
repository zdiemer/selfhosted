#!/usr/bin/env python3
"""Render both releases and catch missing/overridden TLS trust configuration."""
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parent


class NasTrustTests(unittest.TestCase):
    def test_both_rendered_drivers_verify_with_the_committed_trust_bundle(self):
        certificate = (ROOT / 'trust/truenas-api.crt').read_text().strip()
        for backend in ('iscsi', 'nfs'):
            with self.subTest(backend=backend):
                release = 'democratic-csi-' + backend
                docs = list(yaml.safe_load_all(subprocess.check_output([
                    'helm', 'template', release, str(ROOT), '-f',
                    str(ROOT / ('values-' + backend + '.yaml'))], text=True)))
                configmap = next(d for d in docs if d and d['kind'] == 'ConfigMap' and d['metadata']['name'] == release)
                self.assertEqual(configmap['data']['extra-ca-certs'].strip(), certificate)
                workloads = [d for d in docs if d and d['kind'] in ('Deployment', 'DaemonSet')]
                self.assertEqual(len(workloads), 2)
                for workload in workloads:
                    spec = workload['spec']['template']['spec']
                    driver = next(c for c in spec['containers'] if c['name'] == 'csi-driver')
                    env = [e for e in driver['env'] if e['name'] == 'NODE_EXTRA_CA_CERTS']
                    self.assertEqual(env, [{'name': 'NODE_EXTRA_CA_CERTS', 'value': '/tmp/certs/extra-ca-certs.crt'}])
                    self.assertTrue(any(m['name'] == 'extra-ca-certs' and m['mountPath'] == '/tmp/certs' for m in driver['volumeMounts']))
                    self.assertIn('checksum/configmap', workload['spec']['template']['metadata']['annotations'])
                secret = next(d for d in docs if d and d['kind'] == 'Secret' and d['metadata']['name'] == release + '-driver-config')
                config = yaml.safe_load(secret['stringData']['driver-config-file.yaml'])
                self.assertIs(config['httpConnection']['allowInsecure'], False)


    def test_containment_keeps_iscsi_chroot_and_mount_privilege(self):
        # The image wrapper, not its JavaScript, needs /host. Losing this
        # mount passes readiness but breaks the next iSCSI NodeStageVolume.
        for backend in ('iscsi', 'nfs'):
            with self.subTest(backend=backend):
                docs = list(yaml.safe_load_all(subprocess.check_output([
                    'helm', 'template', 'democratic-csi-' + backend, str(ROOT),
                    '-f', str(ROOT / ('values-' + backend + '.yaml')),
                    '--post-renderer', str(ROOT / 'post-render.py')], text=True)))
                ds = next(d for d in docs if d and d['kind'] == 'DaemonSet')
                spec = ds['spec']['template']['spec']
                self.assertIs(spec['hostIPC'], False)
                driver = next(c for c in spec['containers'] if c['name'] == 'csi-driver')
                self.assertTrue(driver['securityContext']['privileged'])
                host_mounts = [m for m in driver['volumeMounts'] if m['name'] == 'host-dir']
                self.assertEqual(bool(host_mounts), backend == 'iscsi')
                if host_mounts:
                    self.assertEqual(host_mounts[0]['mountPath'], '/host')
                for d in docs:
                    if not d or d['kind'] not in ('DaemonSet', 'Deployment'):
                        continue
                    pod = d['spec']['template']['spec']
                    for c in pod['containers'] + pod.get('initContainers', []):
                        sc = c['securityContext']
                        if not sc.get('privileged'):
                            self.assertIs(sc['allowPrivilegeEscalation'], False)
                            self.assertEqual(sc['capabilities'], {'drop': ['ALL']})
                            self.assertEqual(sc['seccompProfile'], {'type': 'RuntimeDefault'})

if __name__ == '__main__':
    unittest.main()
