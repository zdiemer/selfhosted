"""Production must feed the alert pipeline, not just the status dashboard."""
import json
from pathlib import Path
import re
import unittest
import yaml

INFRA = Path(__file__).parents[2]


class WhatnowProbeTests(unittest.TestCase):
    def test_production_has_strict_probe_and_notifying_rule(self):
        config = yaml.safe_load((INFRA / 'alloy/values-probe.yaml').read_text())['alloy']['configMap']['content']
        targets = re.findall(r'target\s*\{([^}]+)\}', config)
        target, = [t for t in targets if 'name    = "whatnow"' in t]
        self.assertIn('address = "https://whatnow.gg/healthz"', target)
        self.assertIn('module  = "http_dependency"', target)
        modules = yaml.safe_load(re.search(r'config = `(.+?)`', config, re.S).group(1))['modules']
        self.assertEqual(modules['http_dependency']['http']['valid_status_codes'], [200])
        rules = json.loads((INFRA / 'grafana-dashboards/alerts/rules.json').read_text())
        rule = next(r for r in rules if r['uid'] == 'sh-probe-down')
        self.assertFalse(rule['isPaused'])
        self.assertEqual(rule['data'][0]['model']['expr'], 'probe_success{cluster="home-k3s",instance!="sms-gateway"}')
        routing = json.loads((INFRA / 'grafana-dashboards/notifications.json').read_text())
        self.assertTrue(routing['enabled'])
        self.assertEqual(routing['receiver'], 'ntfy-primary')


if __name__ == '__main__':
    unittest.main()
