"""Evaluate real firewall expressions with a mock provider, never a cloud API."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('TOFU_TEST_BIN'), 'Set TOFU_TEST_BIN for mock firewall plan test')
class FirewallIntegration(unittest.TestCase):
    def test_planned_ingress_boundaries(self):
        # OpenTofu 1.13.2 test mocks crash on import blocks. Keep production
        # imports intact and test expressions in a disposable copy without them.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack = root / 'provider-iac/digitalocean'
            stack.mkdir(parents=True)
            for name in ['main.tf', 'versions.tf', '.terraform.lock.hcl']:
                shutil.copy(ROOT / 'digitalocean' / name, stack / name)
            (stack / 'tests').mkdir()
            shutil.copy(ROOT / 'tests/firewall.tftest.hcl', stack / 'tests')
            (root / 'whatnow-droplet').mkdir()
            shutil.copy(ROOT.parent / 'whatnow-droplet/config.yml', root / 'whatnow-droplet')
            env = os.environ.copy()
            for key in list(env):
                if key.startswith(('TF_LOG', 'TF_CLI_ARGS')) or key in ['TF_DATA_DIR', 'TF_WORKSPACE']:
                    env.pop(key)
            import sys
            sys.path.insert(0, str(ROOT))
            from run import encryption
            env['TF_ENCRYPTION'] = encryption('offline-test-passphrase-not-for-production')
            # Reuse verified local provider binaries when present.
            cache = ROOT / 'digitalocean/.terraform/providers'
            if cache.is_dir():
                env['TF_PLUGIN_CACHE_DIR'] = str(cache)
            for args in [('init', '-backend=false', '-input=false', '-lockfile=readonly'), ('test', '-no-color')]:
                result = subprocess.run([env['TOFU_TEST_BIN'], '-chdir=' + str(stack), *args],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
