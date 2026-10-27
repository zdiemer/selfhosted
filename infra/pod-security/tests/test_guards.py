import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('guards', Path(__file__).parents[1] / 'guards.py')
guards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guards)

class Guards(unittest.TestCase):
    def test_games_credential_exception_is_scoped(self):
        policy, _ = guards.documents('games')
        rule = next(r['expression'] for r in policy['spec']['validations'] if 'hostPath' in r['expression'])
        for constraint in ['"terraria"', 'serviceAccountName == "terraria-claude"',
                           'v.name == "credentials"', 'v.hostPath.path == "/home/zachd/.claude"',
                           'v.hostPath.type == "Directory"', 'c.name == "claude"',
                           'm.mountPath == "/host-claude"']:
            self.assertIn(constraint, rule)
        self.assertEqual(policy['spec']['failurePolicy'], 'Fail')

    def test_no_terraria_exception_in_other_profiles(self):
        for profile in ['media', 'minecraft', 'talaria']:
            policy, _ = guards.documents(profile)
            rule = next(r['expression'] for r in policy['spec']['validations'] if 'hostPath' in r['expression'])
            self.assertNotIn('terraria-claude', rule)

    def test_dryrun_preserves_credential_service_account(self):
        captured = []
        original = guards.psa.kubectl
        guards.psa.kubectl = lambda *a, **kw: captured.append(kw['body'])
        try:
            template = {'spec': {'serviceAccountName': 'terraria-claude',
                                'containers': [{'name': 'claude', 'image': 'example'}]}}
            guards.dryrun(template, 'scratch')
        finally:
            guards.psa.kubectl = original
        self.assertEqual(captured[0]['spec']['serviceAccountName'], 'terraria-claude')
        self.assertEqual(captured[0]['metadata']['namespace'], 'scratch')

if __name__ == '__main__':
    unittest.main()
