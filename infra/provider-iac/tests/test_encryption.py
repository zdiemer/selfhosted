"""Optional real OpenTofu smoke test, using only built-in local terraform_data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
from run import encryption


@unittest.skipUnless(os.environ.get('TOFU_TEST_BIN'), 'Set TOFU_TEST_BIN for offline encryption integration test')
class EncryptionIntegration(unittest.TestCase):
    def test_encrypted_plan_state_and_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            marker = 'LOCAL-ONLY-ENCRYPTION-CANARY-778812'
            (path / 'main.tf').write_text('''terraform {
  encryption {
    state { enforced = true }
    plan { enforced = true }
  }
}
resource "terraform_data" "canary" { input = "''' + marker + '" }\n')
            env = os.environ.copy()
            env['TF_ENCRYPTION'] = encryption('test-only-passphrase-longer-than-32-characters')
            for key in list(env):
                if key.startswith(('TF_LOG', 'TF_CLI_ARGS')) or key in ['TF_DATA_DIR', 'TF_WORKSPACE']:
                    env.pop(key)
            def run(*args, check=True):
                return subprocess.run([env['TOFU_TEST_BIN'], '-chdir=' + directory, *args],
                                      env=env, check=check, capture_output=True, text=True)
            run('init', '-input=false')
            run('plan', '-input=false', '-out=test.tfplan')
            self.assertNotIn(marker.encode(), (path / 'test.tfplan').read_bytes())
            decoded = json.loads(run('show', '-json', 'test.tfplan').stdout)
            self.assertEqual(decoded['resource_changes'][0]['change']['after']['input'], marker)
            run('apply', '-input=false', 'test.tfplan')
            state = (path / 'terraform.tfstate').read_bytes()
            self.assertNotIn(marker.encode(), state)
            self.assertIn('encrypted_data', json.loads(state))
            env['TF_ENCRYPTION'] = encryption('different-test-passphrase-longer-than-32-characters')
            self.assertNotEqual(run('show', '-json', 'terraform.tfstate', check=False).returncode, 0)
            env.pop('TF_ENCRYPTION')
            self.assertNotEqual(run('plan', '-input=false', check=False).returncode, 0)


if __name__ == '__main__': unittest.main()
