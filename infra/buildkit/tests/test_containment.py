import subprocess
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


class Containment(unittest.TestCase):
    def test_rendered_security_boundary(self):
        objects = list(yaml.safe_load_all(subprocess.check_output(
            ['helm', 'template', 'buildkit', str(ROOT), '-n', 'buildkit'])))
        deployment = next(o for o in objects if o and o['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        self.assertIs(pod['hostUsers'], False)
        self.assertIs(pod['automountServiceAccountToken'], False)
        main = pod['containers'][0]
        self.assertNotIn('--oci-worker-no-process-sandbox', main['args'])
        self.assertEqual(main['securityContext']['runAsUser'], 1000)
        self.assertEqual(main['securityContext']['procMount'], 'Unmasked')
        self.assertEqual(main['securityContext']['seccompProfile']['type'], 'Localhost')
        self.assertEqual(main['securityContext']['appArmorProfile']['type'], 'Localhost')
        for container in pod['containers'] + pod['initContainers']:
            self.assertFalse(container['securityContext'].get('privileged', False))
        self.assertTrue(all('hostPath' not in volume for volume in pod['volumes']))
        self.assertEqual(next(v for v in pod['volumes'] if v['name'] == 'cache')['persistentVolumeClaim']['claimName'], 'buildkit-userns-cache')
        claims = {o['metadata']['name'] for o in objects if o and o['kind'] == 'PersistentVolumeClaim'}
        self.assertEqual(claims, {'buildkit-cache', 'buildkit-userns-cache'})
        subids = next(o['data'] for o in objects if o and o['metadata']['name'] == 'buildkit-subids')
        # RootlessKit inserts build root -> pod user 1000. All remaining mappings
        # must fit Kubernetes' range without overlap or missing common image IDs.
        for value in subids.values():
            ids = [1000]
            for line in value.splitlines():
                _, start, count = line.split(':')
                ids.extend(range(int(start), int(start) + int(count)))
            self.assertEqual(sorted(ids), list(range(65536)))


if __name__ == '__main__':
    unittest.main()
