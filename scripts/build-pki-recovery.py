#!/usr/bin/env python3
"""Back up/restore the internal CA through 1Password without disk staging."""
import base64,json,subprocess,sys
NAME='infra-build-pki-ca'
mode=sys.argv[1] if len(sys.argv)>1 else 'backup'
if mode not in ('backup','restore'): raise SystemExit('Usage: build-pki-recovery.py [backup|restore]')
p=subprocess.run(['kubectl','get','secret','-n','buildkit','internal-build-ca','--ignore-not-found','-o','json'],capture_output=True,check=True)
live={k:base64.b64decode(v).decode() for k,v in json.loads(p.stdout)['data'].items()} if p.stdout.strip() else None
saved=subprocess.run(['op','document','get',NAME,'--vault','homelab'],capture_output=True)
if mode=='backup':
    if not live:raise SystemExit('No live CA to back up')
    if saved.returncode==0:
        if json.loads(saved.stdout)!=live:raise SystemExit('Existing backup differs; refusing overwrite')
    else:
        listing=json.loads(subprocess.check_output(['op','item','list','--vault','homelab','--format=json']))
        if any(i['title']==NAME for i in listing):raise SystemExit('Existing backup cannot be read')
        subprocess.run(['op','document','create','-','--title',NAME,'--file-name','build-ca.json','--vault','homelab'],input=json.dumps(live).encode(),check=True,stdout=subprocess.DEVNULL)
    assert json.loads(subprocess.check_output(['op','document','get',NAME,'--vault','homelab']))==live
    print('CA backup verified')
else:
    if saved.returncode:raise SystemExit('Cannot read the recovery document')
    data=json.loads(saved.stdout)
    if live and live!=data:raise SystemExit('A different live CA exists; refusing replacement')
    if not live:
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'internal-build-ca','namespace':'buildkit'},'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
        subprocess.run(['kubectl','create','-f','-'],input=json.dumps(obj).encode(),check=True,stdout=subprocess.DEVNULL)
    print('CA restored; run the infrastructure upgrades to issue leaves')
