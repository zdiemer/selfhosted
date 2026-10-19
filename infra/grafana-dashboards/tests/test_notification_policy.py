"""Routing checks: only critical infrastructure alerts get independent email."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from notification_policy import notification_settings


class RoutingTest(unittest.TestCase):
    def test_owned_rule_routes(self):
        cfg = json.loads((ROOT / 'notifications.json').read_text()) | {'enabled': True}
        for rule in json.loads((ROOT / 'alerts/rules.json').read_text()):
            with self.subTest(rule=rule['uid']):
                result = notification_settings(rule, cfg)
                if rule['uid'] == 'sh-sms-gateway-down':
                    self.assertEqual(result, {'receiver': 'unwired'})
                else:
                    expected = 'critical-outages' if rule['labels']['severity'] == 'critical' else 'ntfy-primary'
                    if rule['labels'].get('notification_path') == 'email':
                        expected = 'notification-path-email'
                    self.assertEqual(result['receiver'], expected)
                    self.assertEqual(result['repeat_interval'], '4h')
                self.assertEqual(notification_settings(rule, cfg | {'enabled': False}), {'receiver': 'unwired'})


if __name__ == '__main__':
    unittest.main()
