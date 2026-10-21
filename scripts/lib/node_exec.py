"""Short-lived node maintenance, requiring explicit Kubernetes admin access."""
import contextlib, json, subprocess, uuid

@contextlib.contextmanager
def node_session(node):
    name = 'node-maintenance-' + uuid.uuid4().hex[:10]
    spec = {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'infra'},'spec':{'ttlSecondsAfterFinished':60,'activeDeadlineSeconds':600,'backoffLimit':0,'template':{'spec':{'nodeName':node,'hostPID':True,'automountServiceAccountToken':False,'restartPolicy':'Never','containers':[{'name':'maintenance','image':'docker.io/alpine/k8s:1.34.9','command':['sleep','590'],'securityContext':{'privileged':True},'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'memory':'128Mi'}}}]}}}}
    subprocess.run(['kubectl','create','-f','-'],input=json.dumps(spec).encode(),check=True,stdout=subprocess.DEVNULL)
    try:
        subprocess.run(['kubectl','wait','-n','infra','pod','-l','job-name='+name,'--for=condition=Ready','--timeout=90s'],check=True,stdout=subprocess.DEVNULL)
        pod=json.loads(subprocess.check_output(['kubectl','get','pods','-n','infra','-l','job-name='+name,'-o','json']))['items'][0]['metadata']['name']
        def execute(command, data=None):
            return subprocess.run(['kubectl','exec','-i','-n','infra',pod,'--','nsenter','-t','1','-m','-n','--',*command],input=data,check=True,stdout=subprocess.PIPE).stdout
        yield execute
    finally:
        subprocess.run(['kubectl','delete','job','-n','infra',name,'--ignore-not-found','--wait=false'],check=False,stdout=subprocess.DEVNULL)
