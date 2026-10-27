import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('ipc', Path(__file__).parents[1] / 'bridge/ipc.py')
ipc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ipc)

class IPCTests(unittest.TestCase):
    def test_request_scoping_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['actions', 'results']:
                (root / name).mkdir()
            seen = []
            def game():
                deadline = time.time() + 2
                while time.time() < deadline:
                    paths = list((root / 'actions').glob('*.json'))
                    if paths:
                        action = json.loads(paths[0].read_text())
                        seen.append(action)
                        dest = root / 'results' / paths[0].name
                        temporary = dest.with_suffix('.tmp')
                        temporary.write_text(json.dumps({'ok': True, 'value': {'hardmode': False}}))
                        temporary.replace(dest)
                        return
                    time.sleep(.01)
            worker = threading.Thread(target=game)
            worker.start()
            value = ipc.call('context', turn='a' * 32, root=root, timeout=2)
            worker.join()
            self.assertEqual(value, {'hardmode': False})
            self.assertEqual(seen[0]['turn'], 'a' * 32)
            self.assertEqual(seen[0]['op'], 'context')
            self.assertGreater(seen[0]['deadline'], time.time() - 2)
            self.assertEqual(list((root / 'actions').iterdir()), [])
            self.assertEqual(list((root / 'results').iterdir()), [])

    def test_invalid_capability_cannot_escape_directory(self):
        with self.assertRaises(ValueError):
            ipc.call('context', turn='../../somewhere')

    def test_timeout_removes_pending_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['actions', 'results']:
                (root / name).mkdir()
            with self.assertRaises(TimeoutError):
                ipc.call('teleport_home', turn='a' * 32, root=root, timeout=.05)
            self.assertEqual(list((root / 'actions').iterdir()), [])

    def test_chat_limits_and_markup(self):
        chunks = ipc.chat_chunks('[i:123] hi\n' + 'hello ' * 1000)
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(len(c) <= 240 for c in chunks))
        self.assertTrue(chunks[-1].endswith('…'))
        self.assertNotIn('[', ''.join(chunks))
        self.assertNotIn('\n', ''.join(chunks))

if __name__ == '__main__':
    unittest.main()
