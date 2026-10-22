import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('database', Path(__file__).parents[1] / 'database.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RecoverySafety(unittest.TestCase):
    def test_only_scratch_volume_and_readonly_repository(self):
        for target in m.TARGETS:
            with self.subTest(target=target):
                name = 'db-restore-test'
                pvc, policy, job = m.documents(target, 'a' * 64, name)
                pod = job['spec']['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertEqual(pvc['metadata']['name'], name)
                volumes = {v['name']: v for v in pod['volumes']}
                self.assertEqual(volumes['work']['persistentVolumeClaim']['claimName'], name)
                self.assertTrue(volumes['repo']['persistentVolumeClaim']['readOnly'])
                self.assertEqual(policy['spec']['ingress'], [])
                self.assertEqual(policy['spec']['egress'], [])
                self.assertEqual(policy['spec']['podSelector']['matchLabels'], job['spec']['template']['metadata']['labels'])
                database = pod['containers'][0]
                self.assertEqual({v['name'] for v in database['volumeMounts']}, {'work', 'tmp'})
                self.assertTrue(database['securityContext']['runAsNonRoot'])
                self.assertEqual(database['securityContext']['capabilities']['drop'], ['ALL'])
                self.assertNotIn('envFrom', database)
                self.assertIn('RESTORE_VERIFIED', database['command'][-1])
                self.assertTrue(all('hostPath' not in v for v in pod['volumes']))
                self.assertIn('--no-lock --no-cache restore ' + 'a' * 64, pod['initContainers'][0]['command'][-1])

    def test_reject_unpinned_snapshot_or_unsafe_name(self):
        for snapshot in ['latest', 'abc123', 'a' * 63, 'a' * 64 + ';id']:
            with self.assertRaises(ValueError):
                m.documents('romm', snapshot, 'db-restore-test')
        for name in ['production', 'db-restore-;id', '../db-restore-test']:
            with self.assertRaises(ValueError):
                m.documents('romm', 'a' * 64, name)

    def test_sql_truncation_guard_precedes_database_start(self):
        script = (m.ROOT / 'scripts/mariadb.sh').read_text()
        self.assertLess(script.index('INCOMPLETE_SQL_DUMP'), script.index('mariadb-install-db'))
        self.assertIn('--skip-networking', script)
        self.assertIn('--stopOnError', (m.ROOT / 'scripts/mongo.sh').read_text())
        self.assertIn('--exit-on-error', (m.ROOT / 'scripts/postgres.sh').read_text())


if __name__ == '__main__':
    unittest.main()
