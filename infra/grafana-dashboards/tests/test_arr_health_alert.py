"""Check severity separation, alert identities, and transient-warning tolerance.

Run: PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_arr_health_alert.py
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

root = Path(__file__).resolve().parents[1]
rules = {r['uid']: r for r in json.loads((root / 'alerts/rules.json').read_text())}
selected = [rules['sh-arr-health'], rules['sh-arr-health-error']]
tests = []
for name, warning, error in [('healthy', 0, 0), ('warning only', 1, 0),
                             ('error only', 0, 1), ('both severities', 2, 3)]:
    rows = []
    checks = []
    for app, counts in [('prowlarr', (warning, error)), ('sonarr', (0, 0))]:
        for severity, count in zip(('warning', 'error'), counts):
            rows.append({'series': f'home_arr_health_issues{{cluster="home-k3s",app="{app}",severity="{severity}"}}',
                         'values': f'{count}+0x2'})
    for index, rule in enumerate(selected):
        checks.append({'expr': rule['data'][0]['model']['expr'], 'eval_time': '1m',
                       'exp_samples': [
                           {'labels': '{app="prowlarr",cluster="home-k3s"}',
                            'value': (warning, error)[index]},
                           {'labels': '{app="sonarr",cluster="home-k3s"}', 'value': 0}]})
    tests.append({'name': name, 'interval': '1m', 'input_series': rows,
                  'promql_expr_test': checks})

for name, warning_values, error_values, at, firing in [
    ('thirty-minute warning recovers silently', '1+0x29 0+0x40', '0+0x70', '45m', None),
    ('warning still pending before one hour', '1+0x70', '0+0x70', '59m', None),
    ('persistent warning fires', '1+0x70', '0+0x70', '61m', 'sh-arr-health'),
    ('errors retain faster notification', '0+0x70', '1+0x70', '16m', 'sh-arr-health-error'),
]:
    tests.append({'name': name, 'interval': '1m', 'input_series': [
        {'series': f'home_arr_health_issues{{cluster="home-k3s",app="radarr",severity="{severity}"}}',
         'values': values} for severity, values in [('warning', warning_values), ('error', error_values)]],
        'alert_rule_test': [{'eval_time': at, 'alertname': rule['uid'], 'exp_alerts':
                            [{'exp_labels': {'app': 'radarr', **rule['labels']}}]
                            if rule['uid'] == firing else []} for rule in selected]})

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    alert_file = tmp / 'rules.yaml'
    alert_file.write_text(yaml.safe_dump({'groups': [{'name': 'arr-health', 'rules': [
        {'alert': rule['uid'], 'expr': '(' + rule['data'][0]['model']['expr'] + ') > 0',
         'for': rule['for'], 'labels': rule['labels']} for rule in selected]}]}))
    path = tmp / 'test.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [str(alert_file)],
                                   'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
