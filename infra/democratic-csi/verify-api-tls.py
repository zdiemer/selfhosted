#!/usr/bin/env python3
"""Verify every rolled-out CSI driver can authenticate with NAS TLS checking on."""
import subprocess,json,concurrent.futures
pods=json.loads(subprocess.check_output(['kubectl','get','pods','-n','democratic-csi','-o','json']))['items']
pods=[p for p in pods if not p['metadata'].get('deletionTimestamp')]
workloads=json.loads(subprocess.check_output(['kubectl','get','deploy,ds','-n','democratic-csi','-o','json']))['items']
expected=0
for w in workloads:
 if not w['metadata']['name'].startswith('democratic-csi-'):continue
 status=w['status']
 desired=status['desiredNumberScheduled'] if w['kind']=='DaemonSet' else w['spec']['replicas']
 updated=status.get('updatedNumberScheduled',0) if w['kind']=='DaemonSet' else status.get('updatedReplicas',0)
 assert updated==desired,w['metadata']['name']+' is still rolling'
 expected+=desired
pods=[p for p in pods if p['metadata'].get('labels',{}).get('app.kubernetes.io/name')=='democratic-csi']
assert len(pods)==expected,(len(pods),expected)
code='''const fs=require('fs'),https=require('https'),yaml=require('js-yaml');const c=yaml.load(fs.readFileSync('/config/driver-config-file.yaml','utf8')).httpConnection;if(c.allowInsecure!==false)throw Error('TLS verification disabled');https.get({host:c.host,path:'/api/v2.0/system/version',rejectUnauthorized:true,agent:false,headers:{Authorization:'Bearer '+c.apiKey}},r=>{r.resume();if(r.statusCode!==200)process.exit(1);console.log('TLS_AUTHENTICATED')}).on('error',e=>{console.error(e.code);process.exit(1)})'''
def check(p):
 assert all(x['ready'] for x in p['status']['containerStatuses']),p['metadata']['name']
 output=subprocess.check_output(['kubectl','exec','-n','democratic-csi',p['metadata']['name'],'-c','csi-driver','--','node','-e',code],text=True)
 assert 'TLS_AUTHENTICATED' in output
 return p['metadata']['name']
for name in concurrent.futures.ThreadPoolExecutor(max_workers=4).map(check,pods):print(name+' verified',flush=True)
