import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).parents[1] / 'verify-existing-restore.py'
spec = importlib.util.spec_from_file_location('validator', path)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class ExistingRestoreTests(unittest.TestCase):
    def setUp(self):
        self.job = {'metadata': {'name': 'scratch'}, 'status': {'conditions': [{'type':'Failed','status':'True'}]}, 'spec': {'template': {
            'metadata': {'labels': {'app.kubernetes.io/name':'postgres-recovery','recovery-drill':'scratch'}},
            'spec': {'initContainers': [{'name':'restore-archive'}], 'containers': [{'name':'postgres','command':['sh','-ec',r'pg_restore --exit-on-error; DO \\$\\$']}],
                     'volumes':[{'name':'work','persistentVolumeClaim':{'claimName':'scratch'}},{'name':'password','secret':{'secretName':'never-copy'}}]}}}}
        self.logs = 'restored_tables\nANALYZE\nERROR: syntax error'

    def test_reuses_only_scratch_and_denies_network(self):
        policy, job = validator.build_validation(self.job, self.logs, 'verify-scratch')
        pod = job['spec']['template']['spec']
        self.assertEqual([v['name'] for v in pod['volumes']], ['work','tmp'])
        self.assertNotIn('initContainers', pod)
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(policy['spec']['egress'], [])
        self.assertEqual(policy['spec']['ingress'], [])
        command = pod['containers'][0]['command'][-1]
        self.assertNotIn('initdb', command)
        self.assertNotIn('pg_restore', command)
        self.assertIn("<<'SQL'", command)

    def test_remount_permissions_and_kubernetes_dollar_expansion(self):
        _, job = validator.build_validation(self.job, self.logs, 'verify-scratch')
        command = job['spec']['template']['spec']['containers'][0]['command'][-1]
        self.assertIn('chmod 0700 "$PGDATA"', command)
        self.assertNotIn('$$', command)
        self.assertIn('constraints_valid', command)
        self.assertIn('cat /tmp/postgres.log', command)

    def test_refuses_active_job(self):
        self.job['status'] = {}
        with self.assertRaises(ValueError): validator.build_validation(self.job, self.logs, 'verify')

    def test_refuses_restore_failure(self):
        with self.assertRaises(ValueError): validator.build_validation(self.job, 'pg_restore: unexpected EOF', 'verify')

    def test_refuses_unrelated_volume(self):
        self.job['spec']['template']['spec']['volumes'][0]['persistentVolumeClaim']['claimName']='production'
        with self.assertRaises(ValueError): validator.build_validation(self.job, self.logs, 'verify')

    def test_refuses_non_drill(self):
        self.job['spec']['template']['metadata']['labels']['app.kubernetes.io/name']='postgres'
        with self.assertRaises(ValueError): validator.build_validation(self.job, self.logs, 'verify')

if __name__ == '__main__': unittest.main()
