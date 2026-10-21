import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('admission', ROOT / 'apply.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
POLICY = json.loads((ROOT / 'policy.json').read_text())


class Policy(unittest.TestCase):
    def test_default_and_exceptions_are_explicit(self):
        self.assertEqual(module.level('new-app', POLICY), 'baseline')
        self.assertEqual(module.level('fsu', POLICY), 'restricted')
        self.assertEqual(module.level('kubevirt', POLICY), 'privileged')
        self.assertTrue(all(POLICY['exceptions'].values()))
        self.assertEqual(module.level('vmlab', POLICY), 'baseline')
        self.assertEqual(module.level('arc-talaria', POLICY), 'baseline')

    def test_version_pinned_for_all_modes(self):
        labels = module.labels('docs', POLICY)
        for mode in ['enforce', 'audit', 'warn']:
            self.assertEqual(labels[f'pod-security.kubernetes.io/{mode}-version'], 'v1.35')
        self.assertEqual(labels['pod-security.kubernetes.io/enforce'], 'baseline')
        self.assertEqual(labels['pod-security.kubernetes.io/audit'], 'restricted')

    def test_audit_does_not_remove_dangerous_fields(self):
        original = {'spec': {'hostPID': True, 'serviceAccountName': 'app',
                    'containers': [{'name': 'app', 'image': 'test', 'securityContext': {'privileged': True}}],
                    'volumes': [{'name': 'host', 'hostPath': {'path': '/'}}]}}
        pod = module.audit_pod(original, 'scratch')
        self.assertTrue(pod['spec']['hostPID'])
        self.assertTrue(pod['spec']['containers'][0]['securityContext']['privileged'])
        self.assertEqual(pod['spec']['volumes'], original['spec']['volumes'])
        self.assertEqual(original['spec']['serviceAccountName'], 'app')
        self.assertEqual(pod['spec']['serviceAccountName'], 'default')

    def test_dormant_cronjob_and_runner_templates_are_checked(self):
        template = {'spec': {'containers': [{'name': 'x', 'image': 'test'}]}}
        objects = [dict(kind='CronJob', metadata={'namespace': 'app', 'name': 'scheduled'},
                        spec={'jobTemplate': {'spec': {'template': template}}}),
                   dict(kind='AutoscalingRunnerSet', metadata={'namespace': 'ci', 'name': 'runner'},
                        spec={'template': template})]
        self.assertEqual(len(list(module.templates(objects))), 2)

    def test_stateful_claim_becomes_pvc_not_hostpath(self):
        obj = {'kind': 'StatefulSet', 'metadata': {'namespace': 'db', 'name': 'db'},
               'spec': {'template': {'spec': {'containers': [{'name': 'db', 'image': 'test'}]}},
                        'volumeClaimTemplates': [{'metadata': {'name': 'data'}}]}}
        template = next(module.templates([obj]))[2]
        self.assertIn('persistentVolumeClaim', template['spec']['volumes'][0])
        self.assertNotIn('volumes', obj['spec']['template']['spec'])


if __name__ == '__main__': unittest.main()
