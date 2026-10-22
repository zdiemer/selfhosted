#!/usr/bin/env python3
"""Exercise live policies using credential-free probes; always remove test pods."""
import argparse
import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor


def kubectl(*args, input=None):
    return subprocess.check_output(['kubectl', *args], input=input, text=True)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ci', action='store_true', help='Also verify ARC controller and all active runner namespaces')
args = parser.parse_args()
runner_namespaces = []
if args.ci:
    runner_namespaces = [x['metadata']['name'] for x in json.loads(kubectl('get','ns','-l','actions-runner.zachd/repo','-o','json'))['items'] if x['status']['phase']=='Active']
namespaces = ['infra','renovate'] + (['arc-systems'] + runner_namespaces if args.ci else [])
name = 'isolation-check-' + uuid.uuid4().hex[:8]
created = []
probe = '''import socket,json,concurrent.futures,urllib.request
checks={'api':('kubernetes.default.svc',443),'api-backend':('192.168.4.21',6443),'nas':('192.168.4.36',443),'authelia':('authelia.auth.svc',9091),'metadata':('169.254.169.254',80)}
def check(item):
 name,(host,port)=item
 try:
  with socket.create_connection((host,port),timeout=3):pass
  return name,True
 except OSError:return name,False
result=dict(concurrent.futures.ThreadPoolExecutor().map(check,checks.items()))
for name,url in [('github','https://api.github.com'),('npm','https://registry.npmjs.org/renovate/latest')]:
 try:
  with urllib.request.urlopen(url,timeout=5) as r:result[name]=r.status==200
 except OSError:result[name]=False
result['dns']=bool(socket.getaddrinfo('kubernetes.default.svc',443))
print(json.dumps(result))
'''
try:
    # The same worker avoids differences in node routing. Never use host networking.
    node = next(x['metadata']['name'] for x in json.loads(kubectl('get', 'nodes', '-o', 'json'))['items'] if not x['spec'].get('unschedulable') and any(c['type']=='Ready' and c['status']=='True' for c in x['status']['conditions']))
    for ns in namespaces:
        pod = {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':ns},'spec':{
            'nodeName':node,'automountServiceAccountToken':False,'restartPolicy':'Never',
            'securityContext':{'runAsNonRoot':True,'runAsUser':65534,'seccompProfile':{'type':'RuntimeDefault'}},
            'containers':[{'name':'probe','image':'python:3.14.7-alpine','command':['python','-c','from http.server import HTTPServer,BaseHTTPRequestHandler; HTTPServer(("0.0.0.0",9000),BaseHTTPRequestHandler).serve_forever()'],
                'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
                'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}}}]}}
        kubectl('create','-f','-',input=json.dumps(pod))
        created.append(ns)
    for ns in created:
        kubectl('wait','-n',ns,'--for=condition=Ready','pod/'+name,'--timeout=90s')
    def run(ns):
        return ns,json.loads(kubectl('exec','-n',ns,name,'--','python','-c',probe))
    results = dict(ThreadPoolExecutor().map(run,created))
    for ns in namespaces[1:]:
        ip = json.loads(kubectl('get','pod',name,'-n',ns,'-o','json'))['status']['podIP']
        code = "import socket\ntry:\n socket.create_connection((%r,9000),timeout=3).close(); print('allowed')\nexcept OSError: print('blocked')" % ip
        outcome = kubectl('exec','-n','infra',name,'--','python','-c',code).strip()
        assert outcome == 'blocked', (ns,'ingress',outcome)
        print(ns+' ingress: '+outcome)
    print(json.dumps(results,indent=2))
    control = results['infra']
    for target in ['api','api-backend','nas','authelia','github','npm','dns']:
        assert control[target], 'Control destination unavailable: '+target
    for ns in namespaces[1:]:
        expected = {'dns','github','npm'}
        if ns=='arc-systems':
            expected |= {'api','api-backend'}
        for target,allowed in results[ns].items():
            assert allowed == (target in expected), (ns,target,allowed)
    for ns in runner_namespaces:
        # TCP proves the selected BuildKit path. Authentication is tested
        # separately with buildctl using the runner's existing client certificate.
        code = "import socket,urllib.request,urllib.error; socket.create_connection(('buildkitd.buildkit.svc',1234),timeout=5).close()\ntry:\n urllib.request.urlopen('https://registry.zachd.duckdns.org/v2/',timeout=5); raise AssertionError('registry accepted anonymous access')\nexcept urllib.error.HTTPError as e: assert e.code==401,e.code\nprint('BuildKit reachable; registry TLS valid and anonymous access denied')"
        print(ns+': '+kubectl('exec','-n',ns,name,'--','python','-c',code).strip())
    print('POLICIES_VERIFIED (metadata unavailable in control; timeout alone is not proof)')
finally:
    for ns in created:
        subprocess.run(['kubectl','delete','pod',name,'-n',ns,'--wait=false'],check=False)
