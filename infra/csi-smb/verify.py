#!/usr/bin/env python3
"""Read-only mount checks against all four current static SMB claims."""
import subprocess,json,time
import argparse
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--node', default='zachd-ubuntu-6', help='Node to exercise fresh SMB mounts on')
args=ap.parse_args()
jobs=[]
def k(*args,**kw): return subprocess.run(['kubectl',*args],text=True,check=True,**kw)
try:
 for ns,pvc in [('games','cloud-game-library'),('games','romm-library'),('games','romm-saves'),('discord','smitele-bot-matchdata')]:
  name='containment-read-'+pvc
  script='set -eu; test -d /data; f=$(find /data -maxdepth 4 -type f -print -quit); if [ -n "$f" ]; then head -c 4096 "$f" >/dev/null; echo PAYLOAD_READ_OK; else ls /data >/dev/null; echo DIRECTORY_READ_OK; fi'
  job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':ns},'spec':{'backoffLimit':0,'activeDeadlineSeconds':180,'template':{'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':args.node},'containers':[{'name':'reader','image':'busybox:1.37.0','command':['sh','-c',script],'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}},'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'memory':'64Mi'}},'volumeMounts':[{'name':'data','mountPath':'/data','readOnly':True}]}],'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':pvc,'readOnly':True}}]}}}}
  k('apply','-f','-',input=json.dumps(job));jobs.append((ns,name))
 for ns,name in jobs:
  k('-n',ns,'wait','--for=condition=Complete','job/'+name,'--timeout=180s')
  k('-n',ns,'logs','job/'+name)
finally:
 for ns,name in jobs:k('-n',ns,'delete','job',name,'--ignore-not-found')
