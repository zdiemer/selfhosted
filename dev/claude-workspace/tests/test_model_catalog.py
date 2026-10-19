"""Keep mutable workspace tags fresh while caching immutable catalog images."""
import pathlib
import subprocess
import tempfile
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1]
DIGEST = 'sha256:' + 'a' * 64


class ModelCatalogImageTest(unittest.TestCase):
    def render(self, digest=''):
        result = subprocess.run(
            ['helm', 'template', 'claude-workspace', str(CHART), '-f', str(CHART / 'values.yaml'),
             '--set-string', f'happy.modelCatalog.imageDigest={digest}'],
            capture_output=True, text=True)
        return result

    def test_only_catalog_uses_cached_digest(self):
        plain = list(yaml.safe_load_all(self.render().stdout))
        pinned = list(yaml.safe_load_all(self.render(DIGEST).stdout))
        select = lambda docs, kind: next(d for d in docs if d and d['kind'] == kind)
        container = select(pinned, 'CronJob')['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
        self.assertTrue(container['image'].endswith('@' + DIGEST))
        self.assertEqual(container['imagePullPolicy'], 'IfNotPresent')
        original = select(plain, 'CronJob')['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
        self.assertNotIn('@', original['image'])
        self.assertEqual(original['imagePullPolicy'], 'Always')
        self.assertEqual(select(plain, 'Deployment'), select(pinned, 'Deployment'))

    def test_invalid_chart_pin_rejected(self):
        result = self.render('invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be a sha256 digest', result.stderr)

    def preflight(self, headers, enabled='true'):
        # Execute the actual preflight block without registry, vault or Helm
        # mutations. Each invocation must replace any previous release pin.
        script = (CHART / 'upgrade.sh').read_text()
        block = script.split('# A digest-pinned catalog', 1)[1]
        block = '# A digest-pinned catalog' + block.split('# Recover from a half-written', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'headers'
            path.write_bytes(headers.encode())
            return subprocess.run(
                ['bash', '-e', '-c',
                 'MANIFEST_HEADERS="$1"; CATALOG_ENABLED="$2"; REPO=example/workspace; TAG=v40; '
                 'CATALOG_DIGEST=old; VALUE_ARGS=();\n' + block + '\nprintf "%s\\n" "${VALUE_ARGS[@]}"',
                 'test', str(path), enabled], text=True, capture_output=True)

    def test_digest_refreshed_from_final_headers(self):
        result = self.preflight('HTTP/1.1 200 OK\r\nDocker-Content-Digest: ' + DIGEST + '\r\n\r\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('happy.modelCatalog.imageDigest=' + DIGEST, result.stdout)

    def test_missing_or_malformed_digest_fails_closed(self):
        for headers in ['', 'HTTP/1.1 200 OK\r\nDocker-Content-Digest: bad\r\n',
                        'HTTP/1.1 302 Found\r\nDocker-Content-Digest: ' + DIGEST +
                        '\r\n\r\nHTTP/1.1 200 OK\r\n\r\n']:
            with self.subTest(headers=headers):
                self.assertNotEqual(self.preflight(headers).returncode, 0)

    def test_disabled_catalog_needs_no_digest(self):
        result = self.preflight('', enabled='false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('happy.modelCatalog.imageDigest=\n', result.stdout)
        self.assertNotIn('imageDigest=old', result.stdout)


if __name__ == '__main__':
    unittest.main()
