import subprocess,json,re

def run(args,**kw):
 return subprocess.run(args,check=True,capture_output=True,**kw).stdout
items=json.loads(run(['op','item','list','--vault','homelab','--format=json']))
if any(i['title']=='whatnow-production' for i in items):
 raise SystemExit('Item already exists; refusing to overwrite')
env=run(['ssh','whatnow','cat /opt/whatnowgg/.env']).decode()
env=re.sub(r'^WHATNOW_TAG=.*\n?', '',env,flags=re.M)
config=json.loads(run(['ssh','whatnow','cat /root/.docker/config.json']))
config={'auths':{'ghcr.io':config['auths']['ghcr.io']}}
item={'title':'whatnow-production','category':'SECURE_NOTE','fields':[
 {'id':'appenv','label':'app.env','type':'CONCEALED','value':env},
 {'id':'dockerjson','label':'docker.json','type':'CONCEALED','value':json.dumps(config)}]}
run(['op','item','create','--vault','homelab','--format=json'],input=json.dumps(item).encode())
a=run(['op','read','op://homelab/whatnow-production/app.env']).decode()
b=json.loads(run(['op','read','op://homelab/whatnow-production/docker.json']))
assert a.rstrip('\n')==env.rstrip('\n') and b==config
print('Production environment and GHCR credential migrated to homelab vault; round-trip verified. No values printed.')
