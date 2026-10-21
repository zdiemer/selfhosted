"""Check credential isolation and the rendered authenticated client behavior."""
import ast
import io
import ipaddress
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import MagicMock
import urllib.request
import urllib.parse
import yaml

ROOT = Path(__file__).parents[1]
objects = list(yaml.safe_load_all(subprocess.check_output(
    ['helm', 'template', 'cluster-status', str(ROOT)], text=True)))
source = next(o for o in objects if o and o['kind'] == 'ConfigMap'
              and o['metadata']['name'] == 'cluster-status-collector')['data']['collect.py']


class Credentials(unittest.TestCase):
    def test_only_collector_mounts_projected_token(self):
        deployment = next(o for o in objects if o and o['kind'] == 'Deployment')
        spec = deployment['spec']['template']['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        token_volumes = {v['name'] for v in spec['volumes'] if any(
            'serviceAccountToken' in s for s in v.get('projected', {}).get('sources', []))}
        self.assertEqual(token_volumes, {'collector-api'})
        for container in spec['containers']:
            mounted = {m['name'] for m in container['volumeMounts']}
            self.assertEqual(bool(mounted & token_volumes), container['name'] == 'collector')

    def test_stats_grant_without_proxy_or_exec(self):
        role = next(o for o in objects if o and o['kind'] == 'ClusterRole')
        resources = {r for rule in role['rules'] for r in rule['resources']}
        self.assertIn('nodes/stats', resources)
        self.assertFalse(resources & {'nodes/proxy', 'nodes/*', '*', 'pods/exec', 'pods/log', 'secrets'})

    def client(self, **variables):
        selected = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                    and n.name in {'authenticated_get', 'api_get', 'kubelet_stats', 'NoRedirect'}]
        scope = dict(urllib=__import__('urllib'), json=json, ipaddress=ipaddress, **variables)
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'client', 'exec'), scope)
        return scope

    def test_reads_rotated_token_for_each_request(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            opener = MagicMock()
            opener.open.side_effect = lambda *a, **kw: io.BytesIO(b'{}')
            client = self.client(SA=directory, AUTH_OPENER=opener)
            for value in ['first', 'rotated']:
                token.write_text(value)
                client['authenticated_get']('https://kubernetes.default.svc/api/v1/nodes')
            headers = [call.args[0].get_header('Authorization') for call in opener.open.call_args_list]
            self.assertEqual(headers, ['Bearer first', 'Bearer rotated'])

    def test_direct_stats_validates_identity_and_has_no_proxy_fallback(self):
        get = MagicMock(return_value={'node': {'nodeName': 'worker'}})
        client = self.client()
        client['authenticated_get'] = get
        node = {'metadata': {'name': 'worker'}, 'status': {'addresses': [{'type': 'InternalIP', 'address': '192.0.2.1'}]}}
        client['kubelet_stats'](node)
        get.assert_called_once_with('https://192.0.2.1:10250/stats/summary')
        get.return_value = {'node': {'nodeName': 'different'}}
        with self.assertRaises(ValueError): client['kubelet_stats'](node)
        get.side_effect = TimeoutError
        with self.assertRaises(TimeoutError): client['kubelet_stats'](node)
        self.assertEqual(get.call_count, 3)

    def test_redirects_are_not_followed(self):
        handler = self.client()['NoRedirect']()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://other.example'))


if __name__ == '__main__':
    unittest.main()
