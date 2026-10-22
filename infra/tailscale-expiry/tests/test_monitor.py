import importlib.util
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).parents[1] / 'monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = 1800000000


def inventory(seconds, identity='device-1'):
    expiry = datetime.fromtimestamp(NOW + seconds, timezone.utc).isoformat()
    return {'BackendState': 'Running', 'Health': [], 'Self': {'ID': 'self'},
            'Peer': {'public-key': {'ID': identity, 'HostName': 'test', 'KeyExpiry': expiry,
                                    'Online': False}}}


class ExpiryTest(unittest.TestCase):
    def test_each_window_boundary_and_dedup(self):
        data = inventory(8 * 86400)
        expiry = NOW + 8 * 86400
        state = {}
        for index, (seconds, label) in enumerate(m.WINDOWS):
            _, notices = m.plan(data, state, expiry - seconds - 0.01)
            self.assertEqual(notices, [])
            state, notices = m.plan(data, state, expiry - seconds)
            self.assertEqual(len(notices), 1)
            self.assertIn(label, notices[0])
            _, repeated = m.plan(data, state, expiry - seconds)
            self.assertEqual(repeated, [])

    def test_before_boundary(self):
        for seconds, _ in m.WINDOWS:
            data = inventory(seconds)
            state, _ = m.plan(data, {}, NOW - 1)
            _, notices = m.plan(data, state, NOW)
            self.assertEqual(len(notices), 1)

    def test_first_run_or_downtime_only_most_urgent(self):
        state, notices = m.plan(inventory(3600), {}, NOW)
        self.assertEqual(len(notices), 1)
        self.assertIn('12hr window', notices[0])
        self.assertEqual(state['device-1']['stage'], 4)
        self.assertIn('expired window', m.plan(inventory(-1), {}, NOW)[1][0])

    def test_renewal_resets_and_clock_rollback_does_not_resend(self):
        data = inventory(3600)
        state, _ = m.plan(data, {}, NOW)
        state, notices = m.plan(data, state, NOW - 86400)
        self.assertEqual(notices, [])
        self.assertEqual(m.plan(data, state, NOW)[1], [])
        renewed = inventory(90 * 86400)
        state, notices = m.plan(renewed, state, NOW)
        self.assertEqual(notices, [])
        self.assertEqual(len(m.plan(renewed, state, NOW + 83 * 86400)[1]), 1)

    def test_disabled_expiry_and_missing_peer_preserve_other_state(self):
        state = {'gone': {'expiry': 'old', 'stage': 3}, 'device-1': {'stage': 4}}
        data = inventory(3600)
        data['Peer']['public-key'].pop('KeyExpiry')
        updated, notices = m.plan(data, state, NOW)
        self.assertNotIn('device-1', updated)
        self.assertIn('gone', updated)
        self.assertEqual(notices, [])

    def test_failed_delivery_never_persists(self):
        saved = []
        def fail(_):
            raise OSError('unavailable')
        with self.assertRaises(OSError):
            m.check(inventory(3600), {}, NOW, fail, saved.append)
        self.assertEqual(saved, [])
        delivered = []
        m.check(inventory(3600), {}, NOW, delivered.append, saved.append)
        self.assertEqual(len(delivered), 1)
        m.check(inventory(3600), saved[-1], NOW, delivered.append, saved.append)
        self.assertEqual(len(delivered), 1)

    def test_unhealthy_status_and_bad_expiry_fail_closed(self):
        for changes in [{'BackendState': 'NeedsLogin'}, {'Health': ['control disconnected']}]:
            data = inventory(3600)
            data.update(changes)
            with self.assertRaises(RuntimeError):
                m.plan(data, {}, NOW)
        data = inventory(3600)
        data['Peer']['public-key']['KeyExpiry'] = 'bad'
        with self.assertRaises(ValueError):
            m.plan(data, {}, NOW)

    def test_atomic_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state, _ = m.plan(inventory(3600), {}, NOW)
            m.save(path, state)
            self.assertEqual(m.json.loads(path.read_text()), state)
            self.assertEqual(list(path.parent.glob('.state-*')), [])


if __name__ == '__main__':
    unittest.main()
