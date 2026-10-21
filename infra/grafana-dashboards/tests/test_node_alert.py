"""Ensure a healthy cordoned node cannot mask another node's outage."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

rule = next(r for r in json.loads((Path(__file__).resolve().parents[1] / 'alerts/rules.json').read_text()) if r['uid'] == 'sh-node-notready')
expr = rule['data'][0]['model']['expr']
rows = []
for name, ready, cordoned in [('healthy', 1, 0), ('maintenance', 1, 1), ('down', 0, 0), ('down-cordoned', 0, 1)]:
    rows.append({'series': f'kube_node_status_condition{{cluster="home-k3s",node="{name}",condition="Ready",status="true"}}', 'values': f'{ready}+0x5'})
    rows.append({'series': f'kube_node_spec_unschedulable{{cluster="home-k3s",node="{name}"}}', 'values': f'{cordoned}+0x5'})
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp)/'test.yaml'
    path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': [{'interval':'1m','input_series':rows,'promql_expr_test':[{'expr':expr,'eval_time':'3m','exp_samples':[{'labels':f'{{node="{n}"}}','value':v} for n,v in [('healthy',0),('maintenance',0),('down',1),('down-cordoned',1)]]}]}]}))
    subprocess.run([os.environ.get('PROMTOOL','promtool'), 'test','rules',str(path)],check=True)
