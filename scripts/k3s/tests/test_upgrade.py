"""Safety regressions; no cluster access or service restarts."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('upgrade', Path(__file__).parents[1] / 'k3s_upgrade.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def node(name='server0', ver='v1.34.6+k3s1', is_server=True, cordoned=False):
    return {'metadata': {'name': name, 'labels': {'node-role.kubernetes.io/control-plane': ''} if is_server else {}},
            'spec': {'unschedulable': cordoned},
            'status': {'nodeInfo': {'kubeletVersion': ver},
                       'conditions': [{'type': 'Ready', 'status': 'True'}]}}


class VersionSafety(unittest.TestCase):
    def test_server_minor_step(self):
        n = node()
        self.assertEqual(u.validate_step([n], 'server0', 'v1.35.9+k3s1'), n)

    def test_reject_skip_downgrade_major_and_injection(self):
        for target in ('v1.36.1+k3s1', 'v1.33.9+k3s1', 'v2.34.6+k3s1', 'v1.35.9+k3s1; reboot'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                u.validate_step([node()], 'server0', target)

    def test_agent_cannot_advance_past_oldest_server(self):
        nodes = [node(), node('server1', 'v1.35.9+k3s1'), node('worker', is_server=False)]
        with self.assertRaises(ValueError):
            u.validate_step(nodes, 'worker', 'v1.35.9+k3s1')

    def test_server_cannot_skip_peer_minor(self):
        nodes = [node(), node('server1', 'v1.35.9+k3s1')]
        with self.assertRaises(ValueError):
            u.validate_step(nodes, 'server1', 'v1.36.5+k3s1')

    def test_agent_patch_allowed_after_servers(self):
        nodes = [node(ver='v1.35.9+k3s1'), node('worker', is_server=False)]
        u.validate_step(nodes, 'worker', 'v1.35.9+k3s1')

    def test_pressure_or_missing_ready_blocks(self):
        for conditions in ([], [{'type': 'Ready', 'status': 'False'}],
                           [{'type': 'Ready', 'status': 'True'}, {'type': 'DiskPressure', 'status': 'True'}]):
            n = node(); n['status']['conditions'] = conditions
            with self.assertRaises(ValueError):
                u.check_ready([n])


class ExecutionSafety(unittest.TestCase):
    def setup_mocks(self, cordoned=False, old_version=False):
        n = node(cordoned=cordoned)
        fresh = copy.deepcopy(n)
        if not old_version:
            fresh['status']['nodeInfo']['kubeletVersion'] = 'v1.35.9+k3s1'
        def kube(*args):
            if args[:2] == ('get', 'node'): return json.dumps(fresh).encode()
            if 'lease' in args: return json.dumps({'spec': {'renewTime': '2099-01-01T00:00:00Z'}}).encode()
            if args[:2] == ('get', 'nodes'): return json.dumps({'items': [n]}).encode()
            return b''
        def host(name, script, *args, **kwargs):
            if script.startswith('systemctl show'): return b'old-invocation'
            if 'InvocationID' in script: return b'new-invocation'
            return b''
        return n, kube, host

    def run_upgrade(self, n):
        u.execute(n, 'v1.35.9+k3s1', {'stage': '/safe/stage', 'binary_sha256': 'a'*64})

    def test_success_uncordons_after_health_and_releases_lock(self):
        n, kube, host = self.setup_mocks()
        with patch.object(u, 'kube', side_effect=kube) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u, 'cluster_health') as health, patch.object(u, 'workload_health'), patch.object(u.time, 'sleep'):
            self.run_upgrade(n)
            self.assertEqual(health.call_count, 2)
            self.assertIn(unittest.mock.call('uncordon', 'server0'), k.call_args_list)
            self.assertEqual(k.call_args_list[-1], unittest.mock.call('-n', 'kube-system', 'delete', 'configmap', 'k3s-upgrade-lock'))

    def test_preexisting_cordon_preserved(self):
        n, kube, host = self.setup_mocks(cordoned=True)
        with patch.object(u, 'kube', side_effect=kube) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u, 'cluster_health'), patch.object(u, 'workload_health'), patch.object(u.time, 'sleep'):
            self.run_upgrade(n)
            self.assertNotIn(unittest.mock.call('uncordon', 'server0'), k.call_args_list)

    def test_etcd_failure_keeps_cordon_and_lock(self):
        n, kube, host = self.setup_mocks()
        with patch.object(u, 'kube', side_effect=kube) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u, 'cluster_health', side_effect=ValueError('etcd unhealthy')):
            with self.assertRaises(ValueError): self.run_upgrade(n)
            self.assertFalse(any('uncordon' in c.args or 'delete' in c.args for c in k.call_args_list))

    def test_stale_ready_with_old_version_does_not_pass(self):
        n, kube, host = self.setup_mocks(old_version=True)
        with patch.object(u, 'kube', side_effect=kube) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u.time, 'monotonic', side_effect=[0, 1, 301]), patch.object(u.time, 'sleep'):
            with self.assertRaises(ValueError): self.run_upgrade(n)
            self.assertFalse(any('uncordon' in c.args for c in k.call_args_list))

    def test_lock_conflict_prevents_mutation(self):
        n, _, host = self.setup_mocks()
        with patch.object(u, 'kube', side_effect=ValueError('lock exists')), patch.object(u, 'host', side_effect=host) as h:
            with self.assertRaises(ValueError): self.run_upgrade(n)
            self.assertFalse(any('systemd-run' in c.args[1] for c in h.call_args_list))

    def test_bad_staged_checksum_prevents_cordon(self):
        n, _, host = self.setup_mocks()
        def bad_host(name, script, *a, **kw):
            if 'sha256sum -c' in script:
                raise subprocess.CalledProcessError(1, ['sha256sum'])
            return host(name, script)
        with patch.object(u, 'kube') as k, patch.object(u, 'host', side_effect=bad_host):
            with self.assertRaises(subprocess.CalledProcessError): self.run_upgrade(n)
            k.assert_not_called()

    def test_old_lease_cannot_pass_even_with_new_version(self):
        n, kube, host = self.setup_mocks()
        def stale_lease(*args):
            if 'lease' in args: return json.dumps({'spec': {'renewTime': '2020-01-01T00:00:00Z'}}).encode()
            return kube(*args)
        with patch.object(u, 'kube', side_effect=stale_lease) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u.time, 'monotonic', side_effect=[0, 1, 301]), patch.object(u.time, 'sleep'):
            with self.assertRaises(ValueError): self.run_upgrade(n)
            self.assertFalse(any('uncordon' in c.args for c in k.call_args_list))

    def test_application_regression_keeps_cordon(self):
        n, kube, host = self.setup_mocks()
        with patch.object(u, 'kube', side_effect=kube) as k, patch.object(u, 'host', side_effect=host), \
             patch.object(u, 'cluster_health'), patch.object(u, 'workload_health', side_effect=ValueError('workload unavailable')), \
             patch.object(u.time, 'sleep'):
            with self.assertRaises(ValueError): self.run_upgrade(n)
            self.assertFalse(any('uncordon' in c.args or 'delete' in c.args for c in k.call_args_list))

    def test_recovery_inside_git_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.git').mkdir()
            with patch.object(u.sys, 'argv', ['upgrade', '--version', 'v1.35.9+k3s1', '--node', 'server0', '--prepare', '--recovery-dir', str(root / 'backups')]), \
                 patch.object(u, 'kube') as k:
                with self.assertRaises(SystemExit): u.main()
                k.assert_not_called()

    def test_pod_execution_rejected_before_cluster_access(self):
        with patch.object(u.sys, 'argv', ['upgrade', '--version', 'v1.35.9+k3s1', '--node', 'server0', '--execute', '--transport', 'pod']), \
             patch.object(u, 'kube') as k:
            with self.assertRaises(SystemExit): u.main()
            k.assert_not_called()

    def test_corrupt_or_stale_recovery_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'backup.tgz'; archive.write_bytes(b'backup')
            m = dict(node='server0', target='v1.35.9+k3s1', original_version='v1.34.6+k3s1',
                     archive=str(archive), archive_sha256=hashlib.sha256(b'backup').hexdigest(),
                     prepared_at=dt.datetime.now(dt.timezone.utc).isoformat())
            path = root / 'manifest.json'; path.write_text(json.dumps(m))
            u.load_prepared(path, node(), 'v1.35.9+k3s1')
            archive.write_bytes(b'corrupt')
            with self.assertRaises(ValueError): u.load_prepared(path, node(), 'v1.35.9+k3s1')
            archive.write_bytes(b'backup'); m['prepared_at']='2020-01-01T00:00:00+00:00'; path.write_text(json.dumps(m))
            with self.assertRaises(ValueError): u.load_prepared(path, node(), 'v1.35.9+k3s1')


if __name__ == '__main__': unittest.main()
