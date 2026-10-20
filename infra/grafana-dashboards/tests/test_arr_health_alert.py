"""Keep exporter severity from colliding with the alert's routing severity.

Run: PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_arr_health_alert.py
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

root = Path(__file__).resolve().parents[1]
rule = next(r for r in json.loads((root / 'alerts/rules.json').read_text())
            if r['uid'] == 'sh-arr-health')
expr = rule['data'][0]['model']['expr']

tests = []
for name, warning, error in [('healthy', 0, 0), ('warning only', 1, 0),
                             ('error only', 0, 1), ('both severities', 2, 3)]:
    rows = []
    expected = []
    for app, counts in [('prowlarr', (warning, error)), ('sonarr', (0, 0))]:
        for severity, count in zip(('warning', 'error'), counts):
            rows.append({
                'series': 'home_arr_health_issues{cluster="home-k3s",'
                          f'app="{app}",severity="{severity}"}}',
                'values': f'{count}+0x2',
            })
        # Exactly one instance per app, without the conflicting source label.
        expected.append({'labels': f'{{app="{app}",cluster="home-k3s"}}',
                         'value': sum(counts)})
    tests.append({'name': name, 'interval': '1m', 'input_series': rows,
                  'promql_expr_test': [{'expr': expr, 'eval_time': '1m',
                                       'exp_samples': expected}]})

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'test.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m',
                                    'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)],
                   check=True)
