import json, yaml, subprocess, os, tempfile
from pathlib import Path
expr=next(r for r in json.load((Path(__file__).resolve().parents[1] / 'alerts/rules.json').open()) if r['uid']=='sh-cluster-telemetry-missing')['data'][0]['model']['expr']
tests=[]
for name,rows,at,missing in [
 ('healthy',[{'series':'up{cluster="home-k3s"}', 'values':'1+0x20'}],'12m',False),
 ('recent interruption',[{'series':'up{cluster="home-k3s"}', 'values':'1 stale'}],'5m',False),
 ('full outage',[{'series':'up{cluster="home-k3s"}', 'values':'1 stale'}],'12m',True),
 ('no heartbeat ever',[],'12m',True)]:
 samples=[{'labels':'{}','value':0}]
 if missing: samples.append({'labels':'{cluster="home-k3s"}','value':1})
 tests.append({'name':name,'interval':'1m','input_series':rows,'promql_expr_test':[{'expr':expr,'eval_time':at,'exp_samples':samples}]})
with tempfile.TemporaryDirectory() as tmp:
 path = Path(tmp) / 'test.yaml'
 path.write_text(yaml.safe_dump({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
 subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], check=True)
