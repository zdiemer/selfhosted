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
        state, event = monitor.transition(state, False, 1899)
        self.assertIsNone(event)
        state, event = monitor.transition(state, False, 1900)
        self.assertEqual(event, 'down')
        state, event = monitor.transition(state, False, 1900 + 86400 - 1)
        self.assertIsNone(event)
        state, event = monitor.transition(state, False, 1900 + 86400)
        self.assertEqual(event, 'down')

    def test_recovery_silently_rearms_after_sustained_health(self):
        state = {'failed_since': 1000, 'notified': True, 'last_notification': 1300}
        state, event = monitor.transition(state, True, 1400)
        self.assertIsNone(event)
        self.assertEqual(state['healthy_since'], 1400)
        self.assertEqual(monitor.transition(state, True, 2299), (state, None))
        self.assertEqual(monitor.transition(state, True, 2300), ({}, None))
        state, event = monitor.transition({}, False, 2400)
        self.assertIsNone(event)
        self.assertEqual(monitor.transition(state, False, 3300)[1], 'down')

    def test_brief_reconnect_preserves_incident_cooldown(self):
        previous = {'failed_since': 1000, 'notified': True, 'last_notification': 1900}
        state, _ = monitor.transition(previous, True, 2000)
        state, event = monitor.transition(state, False, 2899)
        self.assertEqual((state, event), (previous, None))
        state, _ = monitor.transition(state, True, 3000)
        self.assertEqual(state['healthy_since'], 3000)

    def test_transient_failure_resets(self):
        state, _ = monitor.transition({}, False, 1000)
        self.assertEqual(monitor.transition(state, True, 1100), ({}, None))
        state, event = monitor.transition({}, False, 1200)
        self.assertEqual(state['failed_since'], 1200)
        self.assertIsNone(event)

    def test_transition_preserves_prior_state_until_publish_succeeds(self):
        previous = {'failed_since': 1000}
        state, event = monitor.transition(previous, False, 1900)
        self.assertEqual(event, 'down')
        self.assertNotIn('notified', previous)
        self.assertTrue(state['notified'])


class Delivery(unittest.TestCase):
    def run_monitor(self, state, healthy, fail_publish=False, fallback=False, now=1900):
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
             patch('time.time', return_value=now):
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

    def test_recovery_saves_state_without_publishing(self):
        saved, published = self.run_monitor({'failed_since': 1000, 'notified': True}, True)
        self.assertEqual(published, [])
        state = json.loads(saved[0]['data']['state'])
        self.assertEqual(state['healthy_since'], 1900)
        self.assertTrue(state['notified'])
        saved, published = self.run_monitor(state, True, now=2800)
        self.assertEqual(published, [])
        self.assertEqual(json.loads(saved[0]['data']['state']), {})


if __name__ == '__main__':
    unittest.main()
