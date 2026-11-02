import copy
import importlib.util
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('renderer', ROOT / 'post-render.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class NotificationTests(unittest.TestCase):
    def fixture(self):
        profiles = yaml.safe_load((ROOT / 'values.local.yaml.example').read_text())['config']['profiles.yaml']
        return [
            {'kind': 'ConfigMap', 'metadata': {'name': 'crowdsec-profiles'}, 'data': {'profiles.yaml': profiles}},
            {'kind': 'Deployment', 'spec': {'template': {'metadata': {}, 'spec': {'volumes': [
                {'configMap': {'name': 'crowdsec-profiles'}}]}}}}]

    def test_silent_scans_keep_bans_and_other_notifications(self):
        docs = self.fixture()
        before = list(yaml.safe_load_all(docs[0]['data']['profiles.yaml']))
        scenarios = yaml.safe_load((ROOT / 'values.yaml').read_text())['profiles']['routineScenarios']
        r.patch_profiles(docs, scenarios)
        after = list(yaml.safe_load_all(docs[0]['data']['profiles.yaml']))
        self.assertEqual(before[1:], after[1:])
        self.assertEqual(before[0]['decisions'], after[0]['decisions'])
        self.assertEqual(after[0]['on_success'], 'break')
        self.assertNotIn('notifications', after[0])
        for scenario in ('crowdsecurity/http-path-traversal-probing', 'crowdsecurity/http-cve-probing', 'LePresidente/http-generic-403-bf'):
            self.assertIn(scenario, after[0]['filters'][0])
        self.assertNotIn('ssh-bf', after[0]['filters'][0])
        first = copy.deepcopy(docs)
        r.patch_profiles(docs, scenarios)
        self.assertEqual(first, docs)
        checksum = docs[1]['spec']['template']['metadata']['annotations']['checksum/notification-profiles']
        r.patch_profiles(docs, scenarios + ['example/new-scanner'])
        self.assertNotEqual(checksum, docs[1]['spec']['template']['metadata']['annotations']['checksum/notification-profiles'])

    def test_unexpected_notifying_profile_rejected(self):
        docs = self.fixture()
        docs[0]['data']['profiles.yaml'] = 'name: changed\nnotifications: [sms_relay]\n'
        with self.assertRaises(ValueError):
            r.patch_profiles(docs, [])

    def test_fresh_install_without_notifier_unchanged(self):
        docs = self.fixture()
        docs[0]['data']['profiles.yaml'] = yaml.safe_load((ROOT / 'values.yaml').read_text())['config']['profiles.yaml']
        before = copy.deepcopy(docs)
        r.patch_profiles(docs, [])
        self.assertEqual(before, docs)


if __name__ == '__main__':
    unittest.main()
