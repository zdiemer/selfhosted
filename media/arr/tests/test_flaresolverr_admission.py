import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('runner', Path(__file__).parents[1] / 'files/flaresolverr-runner.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdmissionTest(unittest.TestCase):
    def test_busy_browser_does_not_block_health(self):
        def app(env, start):
            start('200 OK', [])
            yield b'ok'
        handler = module.BrowserAdmission(app, 1)
        active = handler({'PATH_INFO': '/v1'}, lambda *_: None)
        self.assertEqual(next(active), b'ok')
        status = []
        busy = handler({'PATH_INFO': '/v1'}, lambda s, h: status.append((s, dict(h))))
        self.assertIn(b'busy', b''.join(busy))
        self.assertEqual(status[0][0], '503 Service Unavailable')
        self.assertEqual(status[0][1]['Retry-After'], '5')
        self.assertEqual(b''.join(handler({'PATH_INFO': '/health'}, lambda *_: None)), b'ok')
        self.assertEqual(b''.join(active), b'')
        self.assertEqual(b''.join(handler({'PATH_INFO': '/v1'}, lambda *_: None)), b'ok')

    def test_exception_releases_capacity(self):
        def app(env, start):
            raise RuntimeError('browser failed')
        handler = module.BrowserAdmission(app, 1)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                list(handler({'PATH_INFO': '/v1'}, lambda *_: None))

    def test_closed_response_releases_capacity(self):
        handler = module.BrowserAdmission(lambda e, s: iter([b'a', b'b']), 1)
        active = handler({'PATH_INFO': '/v1'}, lambda *_: None)
        next(active)
        active.close()
        self.assertEqual(b''.join(handler({'PATH_INFO': '/v1'}, lambda *_: None)), b'ab')

    def test_abandoned_unstarted_response_does_not_acquire(self):
        handler = module.BrowserAdmission(lambda e, s: [b'ok'], 1)
        abandoned = handler({'PATH_INFO': '/v1'}, lambda *_: None)
        abandoned.close()
        self.assertEqual(b''.join(handler({'PATH_INFO': '/v1'}, lambda *_: None)), b'ok')


if __name__ == '__main__':
    unittest.main()
