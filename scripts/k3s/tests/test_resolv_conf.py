import pathlib
import runpy
import tempfile
import unittest

module = runpy.run_path(str(pathlib.Path(__file__).parents[1] / 'k3s-resolv-conf.py'))


class ResolverTests(unittest.TestCase):
    def test_duplicates_limit_and_search_options(self):
        result = module['bounded_resolv_conf']('''nameserver 50.0.1.1
nameserver 50.0.1.1
nameserver 50.0.2.2
nameserver 2001:5a8::11
nameserver 2001:5a8::33
search taila58f7e.ts.net
options edns0
''')
        self.assertEqual([l for l in result.splitlines() if l.startswith('nameserver')],
                         ['nameserver 50.0.1.1', 'nameserver 50.0.2.2', 'nameserver 2001:5a8::11'])
        self.assertIn('search taila58f7e.ts.net\noptions edns0\n', result)

    def test_unreachable_or_invalid_addresses_are_excluded(self):
        result = module['bounded_resolv_conf']('''nameserver 127.0.0.53
nameserver ::1
nameserver fe80::1%eth0
nameserver 0.0.0.0
nameserver invalid
nameserver 50.0.1.1
''')
        self.assertEqual(result.count('nameserver '), 1)

    def test_refresh_preserves_good_file_on_empty_input_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = [pathlib.Path(directory) / n for n in ('source', 'target')]
            source.write_text('nameserver 50.0.1.1\n')
            module['refresh'](source, target)
            before = target.stat().st_mtime_ns
            module['refresh'](source, target)
            self.assertEqual(before, target.stat().st_mtime_ns)
            source.write_text('nameserver 127.0.0.53\n')
            with self.assertRaises(ValueError):
                module['refresh'](source, target)
            self.assertIn('nameserver 50.0.1.1', target.read_text())


if __name__ == '__main__':
    unittest.main()
