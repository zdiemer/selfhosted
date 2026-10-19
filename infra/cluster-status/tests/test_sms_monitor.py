import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import io
import json
import urllib.error

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).parents[1] / 'scripts/sms-monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class Transitions(unittest.TestCase):
    def test_failure_debounce_and_repeat(self):
        state, event = monitor.transition({}, False, 1000)
        self.assertIsNone(event)
        state, event = monitor.transition(state, False, 1299)
        self.assertIsNone(event)
        state, event = monitor.transition(state, False, 1300)
        self.assertEqual(event, 'down')
        state, event = monitor.transition(state, False, 15699)
        self.assertIsNone(event)
        state, event = monitor.transition(state, False, 15700)
        self.assertEqual(event, 'down')

    def test_recovery_once(self):
        state = {'failed_since': 1000, 'notified': True, 'last_notification': 1300}
        state, event = monitor.transition(state, True, 1400)
        self.assertEqual((state, event), ({}, 'recovered'))
        self.assertEqual(monitor.transition(state, True, 1500), ({}, None))

    def test_transient_failure_resets(self):
        state, _ = monitor.transition({}, False, 1000)
        self.assertEqual(monitor.transition(state, True, 1100), ({}, None))
        state, event = monitor.transition({}, False, 1200)
        self.assertEqual(state['failed_since'], 1200)
        self.assertIsNone(event)

    def test_transition_preserves_prior_state_until_publish_succeeds(self):
        previous = {'failed_since': 1000}
        state, event = monitor.transition(previous, False, 1300)
        self.assertEqual(event, 'down')
        self.assertNotIn('notified', previous)
        self.assertTrue(state['notified'])


class Delivery(unittest.TestCase):
    def run_monitor(self, state, healthy, fail_publish=False, fallback=False):
        saved = []
        published = []
        def request(req, **kwargs):
            if isinstance(req, str):
                if not healthy:
                    raise urllib.error.URLError('connection refused')
                response = io.BytesIO(b'{}')
                response.status = 200
                return response
            if req.full_url == 'http://ntfy/alerts':
                published.append(req)
                if fail_publish:
                    raise urllib.error.URLError('ntfy unavailable')
                return io.BytesIO(b'{"id":"test","event":"message"}')
            if req.full_url == 'http://relay/api/v1/messages':
                response = io.BytesIO(b'{"messages":[]}')
                response.status = 202
                published.append(req)
                return response
            if req.method == 'GET':
                return io.BytesIO(json.dumps({'metadata': {'resourceVersion': '1'},
                    'data': {'state': json.dumps(state)}}).encode())
            saved.append(json.loads(req.data))
            return io.BytesIO(b'{}')
        env = {'STATE_CONFIGMAP': 'state', 'PROBE_URL': 'http://relay/health',
               'NTFY_URL': 'http://ntfy/alerts', 'NTFY_PASSWORD': 'test'}
        if fallback:
            env.update(SMS_RELAY_KEY='key', SMS_RELAY_URL='http://relay', ALERT_TO='+12025550101')
        with patch.dict('os.environ', env), patch.object(Path, 'read_text', return_value='test'), \
             patch('ssl.create_default_context'), patch('urllib.request.urlopen', side_effect=request), \
             patch('time.time', return_value=1300):
            if fail_publish and not fallback:
                with self.assertRaises(urllib.error.URLError):
                    monitor.main()
            else:
                monitor.main()
        return saved, published

    def test_successful_alert_commits_deduplication(self):
        saved, published = self.run_monitor({'failed_since': 1000}, False)
        self.assertEqual(len(published), 1)
        self.assertTrue(json.loads(saved[0]['data']['state'])['notified'])

    def test_failed_publish_does_not_suppress_retry(self):
        saved, published = self.run_monitor({'failed_since': 1000}, False, True)
        self.assertEqual(len(published), 1)
        self.assertEqual(saved, [])

    def test_ntfy_failure_queues_deduplicated_fallback(self):
        saved, published = self.run_monitor({'failed_since': 1000}, False, True, True)
        self.assertEqual(len(published), 2)
        fallback = json.loads(published[1].data)
        self.assertEqual(fallback['idempotency_key'], 'sms-monitor-1000-down-0')
        self.assertTrue(json.loads(saved[0]['data']['state'])['notified'])

    def test_recovery_publishes_before_clearing_state(self):
        saved, published = self.run_monitor({'failed_since': 1000, 'notified': True}, True)
        self.assertEqual(len(published), 1)
        self.assertEqual(json.loads(saved[0]['data']['state']), {})


if __name__ == '__main__':
    unittest.main()
