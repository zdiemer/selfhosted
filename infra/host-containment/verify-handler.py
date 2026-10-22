#!/usr/bin/env python3
"""Exercise virt-handler's real bound-token node restrictions, using dry runs."""
import json
import os
import subprocess

def read(*args):
    return subprocess.check_output(['kubectl', *args], text=True)

pods = json.loads(read('-n', 'kubevirt', 'get', 'pods', '-l', 'kubevirt.io=virt-handler', '-o', 'json'))['items']
pod = next(p for p in pods if p['status']['phase'] == 'Running')
node = pod['spec']['nodeName']
other = next(p['spec']['nodeName'] for p in pods if p['spec']['nodeName'] != node)
# Capture the credential directly into memory, never a file, argument or log.
token = read('-n', 'kubevirt', 'exec', pod['metadata']['name'], '-c', 'virt-handler', '--',
             'cat', '/var/run/secrets/kubernetes.io/serviceaccount/token').strip()
config = json.loads(read('config', 'view', '--raw', '-o', 'json'))
if not config.get('current-context'):
    host = os.environ['KUBERNETES_SERVICE_HOST']
    host = '[' + host + ']' if ':' in host else host
    config = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'cluster',
              'clusters': [{'name': 'cluster', 'cluster': {
                  'server': 'https://' + host + ':' + os.environ.get('KUBERNETES_SERVICE_PORT', '443'),
                  'certificate-authority': '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'}}],
              'contexts': [{'name': 'cluster', 'context': {'cluster': 'cluster'}}]}
else:
    config = json.loads(read('config', 'view', '--raw', '--minify', '-o', 'json'))
config['users'] = [{'name': 'handler-probe', 'user': {'token': token}}]
config['contexts'][0]['context']['user'] = 'handler-probe'
fd = os.memfd_create('handler-verification')
os.write(fd, json.dumps(config).encode())

def call(*args):
    return subprocess.run(['kubectl', '--kubeconfig=/proc/self/fd/'+str(fd), *args],
                          pass_fds=(fd,), text=True, capture_output=True)
try:
    tests = [
        ('own KubeVirt label', node, {'metadata': {'labels': {'kubevirt.io/schedulable': 'true'}}}, True),
        ('another node', other, {'metadata': {'labels': {'kubevirt.io/schedulable': 'true'}}}, False),
        ('cordon own node', node, {'spec': {'unschedulable': True}}, False),
        ('unrelated node label', node, {'metadata': {'labels': {'containment-test': 'blocked'}}}, False),
    ]
    for label, target, patch, allowed in tests:
        r = call('patch', 'node', target, '--type=merge', '--dry-run=server', '-p', json.dumps(patch))
        assert (r.returncode == 0) == allowed, label + ': unexpected authorization result'
        if not allowed:
            assert 'kubevirt-node-restriction' in r.stderr, label + ': not an admission-policy denial'
        print(label, 'allowed (dry run)' if allowed else 'denied by node restriction')
    for verb, resource in [('list', 'secrets'), ('create', 'pods'), ('create', 'clusterrolebindings')]:
        r = call('auth', 'can-i', verb, resource, '-n', 'kubevirt')
        assert r.stdout.strip() == 'no', (verb, resource, 'unexpected grant')
        print(verb, resource, 'denied')
finally:
    os.close(fd)
