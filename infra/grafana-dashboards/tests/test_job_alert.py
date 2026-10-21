"""Exercise the actual Grafana expression with Prometheus' query engine.

Run: PROMTOOL=/path/to/promtool python3 infra/grafana-dashboards/tests/test_job_alert.py
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

root = Path(__file__).resolve().parents[1]
expr = next(r for r in json.loads((root / 'alerts/rules.json').read_text())
            if r['uid'] == 'sh-job-failed')['data'][0]['model']['expr']

def metric(name, value, **labels):
    labels = {'cluster': 'home-k3s', 'namespace': 'test', **labels}
    return {'series': name + '{' + ','.join(f'{k}="{v}"' for k, v in labels.items()) + '}',
            'values': f'{value}+0x3'}

def case(name, terminal=1, owner=None, success=None, prebackup=False):
    rows = [metric('kube_job_failed', terminal, job_name='failed', condition='true'),
            metric('kube_job_status_start_time', 100, job_name='failed')]
    if owner:
        rows += [metric('kube_job_owner', 1, job_name='failed', owner_kind=owner,
                        owner_name='schedule', owner_is_controller='true')]
    if success is not None:
        if owner == 'CronJob':
            rows += [metric('kube_cronjob_status_last_successful_time', success, cronjob='schedule')]
        elif owner == 'Backup':
            job = 'recovery-prebackup' if prebackup else 'recovery'
            rows += [metric('kube_job_status_completion_time', success, job_name=job),
                     metric('kube_job_owner', 1, job_name=job, owner_kind='Backup',
                            owner_name='recovery', owner_is_controller='true')]
    recovered = success is not None and success > 100 and not prebackup
    expected = int(bool(terminal) and not recovered)
    return {'name': name, 'interval': '1m', 'input_series': rows,
            'promql_expr_test': [{'expr': expr, 'eval_time': '1m',
                                 'exp_samples': [{'labels': '{namespace="test",job_name="failed"}',
                                                  'value': expected}]}]}

tests = [case('terminal standalone failure'), case('failed retry in successful job', terminal=0),
         case('CronJob never succeeded', owner='CronJob'),
         case('CronJob recovered', owner='CronJob', success=200),
         case('CronJob success predates failure', owner='CronJob', success=50),
         case('Backup recovered', owner='Backup', success=200),
         case('Backup success predates failure', owner='Backup', success=50),
         case('Prebackup hook cannot resolve failed backup', owner='Backup', success=200, prebackup=True)]

# A telemetry gap must neither resolve a real failure nor resurrect a recovered one.
for recovered in [False, True]:
    gap = case('telemetry gap, recovered=' + str(recovered), owner='CronJob',
               success=200 if recovered else None)
    for row in gap['input_series']:
        row['values'] = row['values'].split('+')[0] + ' stale _ _ _ _ _ _ _ _ _'
    gap['promql_expr_test'][0]['eval_time'] = '7m'
    gap['promql_expr_test'].append({'expr': expr, 'eval_time': '10m', 'exp_samples': []})
    tests.append(gap)

recovery = case('fresh recovery clears a retained failure', owner='CronJob', success=200)
for row in recovery['input_series']:
    row['values'] = ('50 stale _ _ _ _ 200' if 'last_successful_time' in row['series']
                     else row['values'].split('+')[0] + ' stale _ _ _ _ _')
recovery['promql_expr_test'][0]['eval_time'] = '6m'
tests.append(recovery)
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'test.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
