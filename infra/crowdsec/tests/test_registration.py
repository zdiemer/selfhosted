import importlib.util
from pathlib import Path
import subprocess
import unittest

spec = importlib.util.spec_from_file_location('renderer', Path(__file__).parents[1] / 'post-render.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

class RegistrationTests(unittest.TestCase):
    def fixture(self, command=None):
        return [{'kind': 'DaemonSet', 'spec': {'template': {'spec': {'initContainers': [{
            'name': 'wait-for-lapi-and-register', 'command': command or ['sh', '-c', r.REGISTER]
        }]}}}}]

    def test_command_preserved_and_idempotent(self):
        docs = self.fixture()
        r.patch(docs)
        r.patch(docs)
        self.assertEqual(docs[0]['spec']['template']['spec']['initContainers'][0]['command'][2], r.PREFIX + r.REGISTER)

    def test_repeat_same_pod_has_different_identity(self):
        command = 'USERNAME=crowdsec-agent-same\n' + r.PREFIX + 'printf "%s" "$USERNAME"'
        names = [subprocess.check_output(['sh', '-c', command], text=True) for _ in range(2)]
        self.assertNotEqual(*names)
        self.assertTrue(all(n.startswith('crowdsec-agent-same-') for n in names))

    def test_upstream_drift_fails_closed(self):
        with self.assertRaises(ValueError):
            r.patch(self.fixture(['sh', '-c', 'different registration']))
        with self.assertRaises(ValueError):
            r.patch([])

    def test_empty_yaml_documents_do_not_break_helm(self):
        import yaml
        stream = yaml.safe_dump_all([None] + self.fixture() + [None])
        out = subprocess.check_output([str(Path(__file__).parents[1] / 'post-render.py')], input=stream, text=True)
        docs = list(yaml.safe_load_all(out))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['kind'], 'DaemonSet')
