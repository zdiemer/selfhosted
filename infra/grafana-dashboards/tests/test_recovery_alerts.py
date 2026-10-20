"""Regression coverage for false recurring OOMs, recovered jobs, and email gating."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

root = Path(__file__).resolve().parents[1]
rules = {r['uid']: r for r in json.loads((root / 'alerts/rules.json').read_text())}
def expr(uid):
    return rules[uid]['data'][0]['model']['expr']
def sample(name, values, labels=''):
    return {'series': name + '{cluster="home-k3s"' + (',' + labels if labels else '') + '}', 'values': values}
def case(name, rows, query, at, expected):
    return {'name': name, 'interval': '1m', 'input_series': rows,
            'promql_expr_test': [{'expr': query, 'eval_time': at, 'exp_samples': expected}]}
tests = []
for name, values in [('healthy', '1+0x10'), ('unreachable', '0+0x10'), ('missing', None), ('stale', '0 stale')]:
    rows = [] if values is None else [sample('home_ntfy_healthy', values)]
    tests.append(case('email ' + name, rows, expr('sh-ntfy-unreachable'), '10m',
                      [{'labels': '{}', 'value': int(name == 'unreachable')}]))
# One real restart extrapolates above 1 with increase(); never call it recurring.
labels = 'namespace="test",pod="worker",container="worker",instance="exporter"'
for name, values, last_oom, expected in [
    ('one restart', '0+0x10 1+0x19', 1, 0),
    ('two restarts', '0+0x5 1+0x5 2+0x18', 1, 1),
    ('latest exit not OOM', '0+0x5 1+0x5 2+0x18', 0, 0),
    ('old restarts aged out', '2+0x30', 1, 0)]:
    rows = [sample('kube_pod_container_status_restarts_total', values, labels),
            sample('kube_pod_container_status_last_terminated_reason', f'{last_oom}+0x30', labels + ',reason="OOMKilled"')]
    # Duplicate exporters must not double the restart count.
    rows.append(sample('kube_pod_container_status_restarts_total', values, labels.replace('exporter', 'other')))
    tests.append(case(name, rows, 'count((' + expr('sh-container-oom') + ') > 1) or vector(0)', '30m',
                      [{'labels': '{}', 'value': expected}]))
labels = 'namespace="test",cronjob="daily"'
for name, success, suspended, expected in [
    ('never succeeded', None, 0, 1), ('old success', 50, 0, 1),
    ('catch-up recovered', 500, 0, 0), ('suspended', None, 1, 0),
    ('later missed schedule', 50, 0, 1)]:
    rows = [sample('kube_cronjob_next_schedule_time', '100+0x90', labels),
            sample('kube_cronjob_spec_suspend', f'{suspended}+0x90', labels)]
    if success is not None:
        rows.append(sample('kube_cronjob_status_last_successful_time', f'{success}+0x90', labels))
    tests.append(case(name, rows, 'count((' + expr('sh-cronjob-overdue') + ') > 3600) or vector(0)', '90m',
                      [{'labels': '{}', 'value': expected}]))
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'tests.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
