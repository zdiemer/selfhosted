import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'scripts/secrets.sh').read_text()
CHECK = source[source.index('check_probe() {'):source.index('# ------------------------------------------------------------ sync markers')]
spec = importlib.util.spec_from_file_location('detail', ROOT / 'scripts/lib/failure-detail.py')
detail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detail)


class Checks(unittest.TestCase):
    def check(self, inject, read):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / 'test.tpl.yaml'
            template.write_text('{{ op://homelab/test/values.local.yaml }}')
            script = CHECK + '\n' + '''
sleep() { :; }
timeout() { shift; "$@"; }
attempts=0
inject_probe() { attempts=$((attempts + 1)); INJECT; }
op() { READ; }
check_probe "$1" "$2"
rc=$?
echo attempts=$attempts
exit $rc
'''.replace('INJECT', inject).replace('READ', read)
            return subprocess.run(['bash', '-c', script, 'test', str(template), tmp+'/out'], capture_output=True, text=True)

    def test_transient_lookup_recovers(self):
        r = self.check('[[ $attempts -ge 2 ]]', 'echo secret')
        self.assertEqual(r.returncode, 0)
        self.assertIn('attempts=2', r.stdout)
        self.assertNotIn('secret', r.stdout)

    def test_permanent_lookup_fails(self):
        r = self.check('return 1', 'echo secret')
        self.assertEqual(r.returncode, 1)
        self.assertIn('UNRESOLVABLE', r.stdout)
        self.assertIn('attempts=3', r.stdout)

    def test_failed_read_is_not_reported_as_empty(self):
        r = self.check('return 0', 'echo private-error >&2; return 1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('READ FAILED', r.stdout)
        self.assertNotIn('EMPTY FIELD', r.stdout)
        self.assertNotIn('private-error', r.stdout+r.stderr)

    def test_empty_value_still_fails(self):
        r = self.check('return 0', 'return 0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('EMPTY FIELD', r.stdout)

    def test_transient_read_recovers(self):
        r = self.check('return 0', '[[ $attempts -ge 2 ]] || return 1; echo secret')
        self.assertEqual(r.returncode, 0)
        self.assertIn('attempts=2', r.stdout)

    def test_notification_keeps_actual_failure(self):
        text = '  UNRESOLVABLE infra/sms-relay/values.local.yaml\n' + 'ok another-chart\n'*45
        text += '42 chart(s) checked\nMain process exited\nFailed with result\nConsumed 9s CPU time'
        self.assertEqual(detail.summarize(text), 'UNRESOLVABLE infra/sms-relay/values.local.yaml')

    def test_notification_preserves_beginning_of_long_error(self):
        result = detail.summarize('ERROR: ' + 'x'*600)
        self.assertTrue(result.startswith('ERROR: '))
        self.assertEqual(len(result), 400)


if __name__ == '__main__':
    unittest.main()
