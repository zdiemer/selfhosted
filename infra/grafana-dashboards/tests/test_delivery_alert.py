"""Run with PROMTOOL=/path/to/promtool; use the actual deployed expression."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

expr = next(r for r in json.loads((Path(__file__).resolve().parents[1] / 'alerts/rules.json').read_text())
            if r['uid'] == 'sh-notification-delivery')['data'][0]['model']['expr']
tests = []
for failed in ['home_relay_failed_last_hour', 'home_relay_fallback_last_hour']:
    check = 'failed' if failed == 'home_relay_failed_last_hour' else 'fallback'
    tests.append({'name': check + ' must not be masked by a healthy queue', 'input_series': [
        {'series': 'home_relay_oldest_pending_seconds{cluster="home-k3s"}', 'values': '0+0x5'},
        {'series': failed + '{cluster="home-k3s"}', 'values': '1+0x5'}],
        'promql_expr_test': [{'expr': '(' + expr + ') > 0', 'eval_time': '1m',
                             'exp_samples': [{'labels': '{check="' + check + '",cluster="home-k3s"}', 'value': 1}]}]})
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'test.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
