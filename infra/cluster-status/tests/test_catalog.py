"""Exercise actual rendered collector code, including no-Ingress discovery."""
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch
import urllib.error
import yaml

ROOT = Path(__file__).parents[1]
manifest = subprocess.check_output(['helm', 'template', 'cluster-status', str(ROOT)], text=True)
config = next(o for o in yaml.safe_load_all(manifest) if o and o['kind'] == 'ConfigMap'
              and o['metadata']['name'] == 'cluster-status-collector')
extras = types.ModuleType('extras')
exec(compile(config['data']['extras.py'], 'extras.py', 'exec'), extras.__dict__)
probes = types.ModuleType('probes')
exec(compile(config['data']['probes.py'], 'probes.py', 'exec'), probes.__dict__)


class Catalog(unittest.TestCase):
    def test_covers_workers_and_cronjobs_without_ingress(self):
        workloads = [dict(namespace='app', name=n, desired=1, ready=1) for n in ['web', 'worker']]
        entries = [dict(name='Site', namespace='app', service='http', workload='web')]
        crons = [dict(namespace='app', name='backup', schedule='0 1 * * *')]
        result = extras.build_catalog(entries, [], workloads, crons, [], {})
        self.assertEqual({r.get('workload') for r in result if r.get('workload')}, {'web', 'worker'})
        self.assertEqual(len(result), 3)
        self.assertEqual(next(r for r in result if r.get('cronjob'))['cronjob'], 'backup')

    def test_external_check_is_health_source_and_failure_wins(self):
        self.assertEqual(extras._catalog_state([], {'ok': True, 'status': 200}, None)[0], 'ok')
        self.assertEqual(extras._catalog_state([{'desired': 1, 'ready': 1}], {'ok': False}, None)[0], 'down')
        self.assertEqual(extras._catalog_state([], None, None)[0], 'unknown')

    def test_ingress_fallback_does_not_duplicate_workload(self):
        rows = [dict(namespace='app', backend='web:80', host='example.com', path='/')]
        result = extras.build_catalog([], rows, [dict(namespace='app', name='web', desired=1, ready=1)], [], [], {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['workload'], 'web')

    def test_shared_host_preserves_service_paths(self):
        rows = [dict(namespace='fsu', backend='terminal:80', host='fsu.example.com', path=p)
                for p in ['/one', '/two']]
        entries = [dict(name=p, namespace='fsu', service='terminal', paths=[p]) for p in ['/one', '/two']]
        result = extras.build_catalog(entries, rows, [], [], [], {})
        self.assertEqual({r['url'] for r in result}, {'https://fsu.example.com/one', 'https://fsu.example.com/two'})
        self.assertEqual(len(result), 2)

    def test_namespace_collisions_and_failed_workers_are_visible(self):
        workloads = [dict(namespace=n, name='worker', desired=1, ready=0) for n in ['a', 'b']]
        result = extras.build_catalog([], [], workloads, [], [], {})
        self.assertEqual(len({r['slug'] for r in result}), 2)
        self.assertTrue(all(r['state'] == 'down' for r in result))


class Probes(unittest.TestCase):
    def setUp(self):
        probes._CACHE.clear()

    def test_ssh_banner_cache_and_redaction(self):
        check = dict(name='droplet', url='ssh://192.0.2.1:22', intervalSeconds=60)
        with patch.object(probes.socket, 'create_connection') as connect:
            connect.return_value.__enter__.return_value.recv.return_value = b'SSH-2.0-OpenSSH\r\n'
            first = probes.probe(check)
            second = probes.probe(check, False)
        self.assertTrue(first['ok'])
        self.assertNotIn('url', second)
        self.assertEqual(first['checkedAt'], second['checkedAt'])
        connect.assert_called_once()

    def test_wrong_banner_and_timeout_fail(self):
        with patch.object(probes.socket, 'create_connection') as connect:
            connect.return_value.__enter__.return_value.recv.return_value = b'HTTP/1.1 200 OK'
            self.assertFalse(probes.probe(dict(url='ssh://192.0.2.1'))['ok'])
            connect.side_effect = TimeoutError
            self.assertEqual(probes.probe(dict(url='ssh://192.0.2.2'))['reason'], 'TimeoutError')

    def test_http_error_is_down_and_not_an_exception(self):
        with patch.object(probes.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('https://example.com', 503, 'bad', {}, None)):
            result = probes.run_checks([dict(name='app', url='https://example.com')])
        self.assertFalse(result[0]['ok'])
        self.assertEqual(result[0]['status'], 503)

    def test_bad_config_does_not_abort_other_checks(self):
        results = probes.run_checks([dict(name='bad', url='ssh://192.0.2.1', timeoutSeconds='bad')])
        self.assertFalse(results[0]['ok'])

    def test_no_minecraft_connection_probe(self):
        values = yaml.safe_load((ROOT / 'values.yaml').read_text())
        self.assertFalse(any(':25565' in c['url'] for c in values['serviceChecks']))


if __name__ == '__main__':
    unittest.main()
