"""Verify sparse discard metrics without masking missing usage telemetry.

Run with PROMTOOL=/path/to/promtool python3 tests/test_ingestion_alert.py.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

rule = next(
    r for r in json.loads((Path(__file__).resolve().parents[1] / 'alerts/rules.json').read_text())
    if r['uid'] == 'sh-metrics-ingestion-rejected'
)
assert rule['noDataState'] == 'NoData'
assert rule['execErrState'] == 'Error'
expr = rule['data'][0]['model']['expr']
heartbeat = {'series': 'grafanacloud_instance_samples_per_second', 'values': '100+0x10'}
discard = {'series': 'grafanacloud_instance_samples_discarded_per_second{reason="sample_duplicate_timestamp"}', 'values': '2+0x10'}
limit = {'series': 'grafanacloud_instance_samples_discarded_per_second{reason="per_user_series_limit"}', 'values': '3+0x10'}
tests = []
for name, inputs, expected in [
    ('absent discard with usage', [heartbeat], [{'labels': '{}', 'value': 0}]),
    ('zero ingestion still proves telemetry exists', [dict(heartbeat, values='0+0x10')], [{'labels': '{}', 'value': 0}]),
    ('missing all telemetry', [], []),
    ('stale usage cannot manufacture zero', [dict(heartbeat, values='100 stale')], []),
    ('rejections retain reasons', [heartbeat, discard, limit], [
        {'labels': '{reason="per_user_series_limit"}', 'value': 3},
        {'labels': '{reason="sample_duplicate_timestamp"}', 'value': 2},
    ]),
    ('rejections without usage remain visible', [discard], [
        {'labels': '{reason="sample_duplicate_timestamp"}', 'value': 2},
    ]),
    ('explicit zero discards', [heartbeat, dict(discard, values='0+0x10')], [
        {'labels': '{reason="sample_duplicate_timestamp"}', 'value': 0},
    ]),
]:
    tests.append({'name': name, 'interval': '1m', 'input_series': inputs,
                  'promql_expr_test': [{'expr': expr, 'eval_time': '10m', 'exp_samples': expected}]})

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'tests.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
