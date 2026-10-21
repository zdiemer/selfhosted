#!/usr/bin/env python3
"""Native admission guardrails for application namespaces with PSA exceptions."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import time
import uuid

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('psa', ROOT / 'apply.py')
psa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(psa)
PROFILES = ['games', 'media', 'minecraft', 'talaria']
BASE_CAPS = ['AUDIT_WRITE','CHOWN','DAC_OVERRIDE','FOWNER','FSETID','KILL','MKNOD',
             'NET_BIND_SERVICE','SETFCAP','SETGID','SETPCAP','SETUID','SYS_CHROOT']


def label(key, value):
    return f'has(object.metadata.labels) && {json.dumps(key)} in object.metadata.labels && object.metadata.labels[{json.dumps(key)}] == {json.dumps(value)}'


def documents(profile, namespace=None, name=None):
    namespace = namespace or profile
    name = name or 'application-guard-' + profile
    steam = '(' + label('app.kubernetes.io/name', 'steam-headless') + ')'
    vpn = '(' + label('app.kubernetes.io/instance', 'arr') + ' && ' + label('app.kubernetes.io/component', 'qbittorrent') + ')'
    recovery = '(' + label('app.kubernetes.io/name', 'postgres-recovery') + ')'
    bridge = '(' + label('app.kubernetes.io/name', 'claude-bridge') + ' && object.spec.serviceAccountName == "claude-bridge")'
    cap_exception = 'false'
    if profile == 'media': cap_exception = f'({vpn} && c.name == "gluetun" && cap == "NET_ADMIN")'
    if profile == 'talaria':
        # The in-progress isolated drill predates the baseline-compatible helper.
        cap_exception = f'({recovery} && c.name == "restore-archive" && cap == "DAC_READ_SEARCH")'
    hostpath = 'false'
    if profile == 'minecraft':
        hostpath = f'({bridge} && v.name == "claude-creds" && v.hostPath.path == "/home/zachd/.claude" && v.hostPath.type == "Directory")'
    hostport = 'false'
    if profile == 'games':
        hostport = f'({steam} && c.name == "steam-headless" && p.containerPort == p.hostPort && ((p.protocol == "TCP" && p.hostPort in [47984,47989,48010]) || (p.protocol == "UDP" && p.hostPort in [47998,47999,48000,48002,48010])))'
    checks = [
        ('(!has(object.spec.hostPID) || !object.spec.hostPID) && (!has(object.spec.hostIPC) || !object.spec.hostIPC) && (!has(object.spec.hostNetwork) || !object.spec.hostNetwork)', 'Host namespaces are forbidden.'),
        ('variables.containers.all(c, !has(c.securityContext) || !has(c.securityContext.privileged) || !c.securityContext.privileged)', 'Privileged containers are forbidden, including init and ephemeral containers.'),
        (f'!has(object.spec.volumes) || object.spec.volumes.all(v, !has(v.hostPath) || {hostpath})', 'Only the declared Claude bridge credential hostPath is permitted.'),
        (f'variables.containers.all(c, !has(c.ports) || c.ports.all(p, !has(p.hostPort) || p.hostPort == 0 || {hostport}))', 'Only the declared Steam streaming host ports are permitted.'),
        (f'variables.containers.all(c, !has(c.securityContext) || !has(c.securityContext.capabilities) || !has(c.securityContext.capabilities.add) || c.securityContext.capabilities.add.all(cap, cap in {json.dumps(BASE_CAPS)} || {cap_exception}))', 'Capabilities exceed the baseline or declared application exception.'),
        ('variables.contexts.all(s, !has(s.seccompProfile) || s.seccompProfile.type in ["RuntimeDefault","Localhost"])', 'Unconfined seccomp is forbidden.'),
        ('variables.contexts.all(s, !has(s.appArmorProfile) || s.appArmorProfile.type in ["RuntimeDefault","Localhost"])', 'Unconfined AppArmor is forbidden.'),
        ('!has(object.metadata.annotations) || object.metadata.annotations.all(k, !k.startsWith("container.apparmor.security.beta.kubernetes.io/") || object.metadata.annotations[k] == "runtime/default" || object.metadata.annotations[k].startsWith("localhost/"))', 'Unconfined legacy AppArmor is forbidden.'),
        ('variables.containers.all(c, !has(c.securityContext) || !has(c.securityContext.procMount) || c.securityContext.procMount == "Default")', 'Unmasked proc mounts are forbidden.'),
        ('!has(object.spec.securityContext) || !has(object.spec.securityContext.sysctls) || size(object.spec.securityContext.sysctls) == 0', 'Pod sysctl overrides are forbidden in guarded application namespaces.'),
        ('variables.contexts.all(s, !has(s.windowsOptions) || !has(s.windowsOptions.hostProcess) || !s.windowsOptions.hostProcess)', 'Windows host-process containers are forbidden.'),
    ]
    policy = {'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicy',
              'metadata':{'name':name},'spec':{'failurePolicy':'Fail',
              'matchConstraints':{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}},
               'resourceRules':[{'apiGroups':[''],'apiVersions':['v1'],'operations':['CREATE','UPDATE'],'resources':['pods','pods/ephemeralcontainers']}]},
              'variables':[{'name':'containers','expression':'object.spec.containers + (has(object.spec.initContainers) ? object.spec.initContainers : []) + (has(object.spec.ephemeralContainers) ? object.spec.ephemeralContainers : [])'},
                           {'name':'contexts','expression':'variables.containers.filter(c, has(c.securityContext)).map(c, c.securityContext) + (has(object.spec.securityContext) ? [object.spec.securityContext] : [])'}],
              'validations':[{'expression':expr,'message':message} for expr,message in checks]}}
    binding = {'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicyBinding',
               'metadata':{'name':name},'spec':{'policyName':name,'validationActions':['Deny','Audit']}}
    return [policy,binding]


def apply(objects):
    result = psa.kubectl('apply','-f','-',body={'apiVersion':'v1','kind':'List','items':objects})
    if result.returncode: raise RuntimeError(result.stderr)
    name=objects[0]['metadata']['name']
    for _ in range(30):
        result=psa.kubectl('get','validatingadmissionpolicy',name,'-o','json')
        obj=json.loads(result.stdout)
        status=obj.get('status',{})
        if status.get('observedGeneration') == obj['metadata']['generation']:
            warnings=status.get('typeChecking',{}).get('expressionWarnings',[])
            if warnings: raise RuntimeError(json.dumps(warnings))
            return
        time.sleep(0.5)
    raise RuntimeError('Policy type checking did not complete: '+name)


def dryrun(template, namespace):
    pod=psa.audit_pod(template,namespace)
    if template['spec'].get('serviceAccountName') == 'claude-bridge':
        pod['spec']['serviceAccountName']='claude-bridge'
    return psa.kubectl('create','--dry-run=server','-f','-',body=pod)


def negative_cases():
    base={'spec':{'containers':[{'name':'untrusted','image':'busybox:1.37'}]}}
    mutations=[('privileged',lambda s:s['containers'][0].update(securityContext={'privileged':True})),
               ('hostPID',lambda s:s.update(hostPID=True)),
               ('hostNetwork',lambda s:s.update(hostNetwork=True)),
               ('hostPath',lambda s:s.update(volumes=[{'name':'root','hostPath':{'path':'/'}}])),
               ('capability',lambda s:s['containers'][0].update(securityContext={'capabilities':{'add':['SYS_ADMIN']}})),
               ('hostPort',lambda s:s['containers'][0].update(ports=[{'containerPort':1234,'hostPort':1234}])),
               ('seccomp',lambda s:s.update(securityContext={'seccompProfile':{'type':'Unconfined'}})),
               ('init privilege',lambda s:s.update(initContainers=[{'name':'init','image':'busybox:1.37','securityContext':{'privileged':True}}]))]
    for name,mutate in mutations:
        pod=copy.deepcopy(base); mutate(pod['spec']); yield name,pod


def verify_denials(namespace):
    for name,pod in negative_cases():
        # Policy/binding admission caches converge asynchronously on API servers.
        # Retrying a dry run cannot persist the deliberately unsafe test pod.
        for attempt in range(30):
            result=dryrun(pod,namespace)
            if result.returncode!=0 and 'application-guard-' in result.stderr:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f'{namespace}: expected guard rejection for {name}: {result.stderr[-700:]}')
    print(namespace, '8 dangerous pod configurations denied')


def verify_ephemeral_denial(namespace):
    result=psa.kubectl('get','pods','-n',namespace,'-o','json')
    pods=json.loads(result.stdout)['items']
    pod=next(p for p in pods if p.get('status',{}).get('phase')=='Running' and not p['metadata'].get('deletionTimestamp'))
    containers=pod['spec'].get('ephemeralContainers',[])+[{
        'name':'guard-denial-'+uuid.uuid4().hex[:8], 'image':'busybox:1.37',
        'command':['true'], 'securityContext':{'privileged':True}}]
    result=psa.kubectl('patch','pod',pod['metadata']['name'],'-n',namespace,
        '--subresource=ephemeralcontainers','--type=merge','--dry-run=server',
        '-p',json.dumps({'spec':{'ephemeralContainers':containers}}))
    if result.returncode==0 or 'application-guard-' not in result.stderr:
        raise RuntimeError(namespace+': expected privileged ephemeral container denial')
    print(namespace,'privileged ephemeral container denied (dry run)')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    objects=psa.get('deployments,statefulsets,daemonsets,cronjobs,jobs,pods')
    objects+=psa.get('prebackuppods.k8up.io')
    for profile in PROFILES:
        namespace='application-guard-check-'+uuid.uuid4().hex[:8]
        name='application-guard-test-'+uuid.uuid4().hex[:8]
        docs=documents(profile,namespace,name)
        result=psa.kubectl('create','-f','-',body={'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,'labels':{'pod-security.kubernetes.io/enforce':'privileged'}}})
        if result.returncode:raise RuntimeError(result.stderr)
        try:
            if profile=='minecraft':
                result=psa.kubectl('create','serviceaccount','claude-bridge','-n',namespace)
                if result.returncode:raise RuntimeError(result.stderr)
            apply(docs)
            count=0
            for ns,workload,template in psa.templates(objects):
                if ns!=profile:continue
                result=dryrun(template,namespace)
                if result.returncode:raise RuntimeError(f'{ns}/{workload}: {result.stderr[:1000]}')
                count+=1
            verify_denials(namespace)
            print(profile, count, 'live pod/controller templates admitted')
        finally:
            psa.kubectl('delete','-f','-','--ignore-not-found',body={'apiVersion':'v1','kind':'List','items':docs})
            psa.kubectl('delete','namespace',namespace,'--wait=false')
    if args.apply:
        for profile in PROFILES:
            apply(documents(profile));verify_denials(profile);verify_ephemeral_denial(profile)
            print(profile,'guard enforced')
    else:print('Preflight passed. Use --apply to enforce.')


if __name__=='__main__':main()
