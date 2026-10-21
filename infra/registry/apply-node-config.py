#!/usr/bin/env python3
"""Apply registry trust/config sequentially using short-lived node jobs."""
import base64,json,pathlib,subprocess,sys,time,yaml
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'scripts/lib'))
from node_exec import node_session

def get(kind,name,ns=None):
 cmd=['kubectl','get',kind]+([name] if name else [])+['-o','json']
 if ns:cmd+=['-n',ns]
 return json.loads(subprocess.check_output(cmd))
config=base64.b64decode(get('secret','registry-node-config','infra')['data']['registries.yaml']).decode()
ca=base64.b64decode(get('secret','registry-internal-tls','infra')['data']['ca.crt']).decode()
code='''import json,pathlib,subprocess,sys,time
payload=json.load(sys.stdin);files=payload['files'];changed=False;old={}
for name,text in files.items():
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True)
 old[name]=p.read_bytes() if p.exists() else None
 if old[name]==text.encode():continue
 temp=p.with_suffix('.new');temp.write_text(text);temp.chmod(0o600);temp.replace(p);changed=True
if not changed: print('unchanged')
unit='k3s' if pathlib.Path('/etc/systemd/system/k3s.service').exists() else 'k3s-agent'
try:
 if changed: subprocess.run(['systemctl','restart',unit],check=True,timeout=120)
 subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
 print('service active')
except Exception:
 for name,value in old.items():
  p=pathlib.Path(name)
  if value is not None:p.write_bytes(value);p.chmod(0o600)
 subprocess.run(['systemctl','restart',unit],timeout=120)
 raise
import ssl,urllib.request,base64
ctx=ssl.create_default_context(cafile='/etc/rancher/k3s/registry-ca.crt')
request=urllib.request.Request('https://10.43.200.10:5000/v2/',headers={'Authorization':'Basic '+base64.b64encode(payload['auth'].encode()).decode()})
with urllib.request.urlopen(request,context=ctx,timeout=15) as response:assert response.status==200
print('verified TLS registry authentication')
if payload['probe']:
 result=subprocess.run(['k3s','crictl','pull',payload['probe']],capture_output=True,timeout=120)
 if result.returncode:raise RuntimeError('CRI image pull failed')
 print('CRI image pull passed')
print('node-configuration-verified',flush=True)
'''
for node in sorted(n['metadata']['name'] for n in get('nodes','')['items']):
 subprocess.run(['kubectl','wait','nodes','--all','--for=condition=Ready','--timeout=180s'],check=True,stdout=subprocess.DEVNULL)
 with node_session(node) as execute:
  for attempt in range(4):
   try:
    output=execute(['python3','-c',code],json.dumps({'files':{'/etc/rancher/k3s/registries.yaml':config,'/etc/rancher/k3s/registry-ca.crt':ca},'auth':yaml.safe_load(config)['configs']['10.43.200.10:5000']['auth']['username']+':'+yaml.safe_load(config)['configs']['10.43.200.10:5000']['auth']['password'],'probe':sys.argv[1] if len(sys.argv)>1 else ''}).encode())
    if b'node-configuration-verified' not in output: raise RuntimeError('exec stream interrupted during restart')
    break
   except (subprocess.CalledProcessError,RuntimeError):
    if attempt == 3: raise
    # Restarting k3s closes exec streams; rerun this idempotent check after recovery.
    time.sleep(10)
  print(node+': '+output.decode().strip(),flush=True)
 subprocess.run(['kubectl','wait','node',node,'--for=condition=Ready','--timeout=180s'],check=True,stdout=subprocess.DEVNULL)
