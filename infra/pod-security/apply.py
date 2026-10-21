#!/usr/bin/env python3
"""Preflight pod admission, then optionally reconcile namespace security labels."""
import argparse
import concurrent.futures
import copy
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent


def kubectl(*args, body=None):
    return subprocess.run(['kubectl', *args], input=json.dumps(body) if body is not None else None,
                          text=True, capture_output=True)


def get(resource):
    result = kubectl('get', resource, '-A', '-o', 'json')
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)['items']


def level(namespace, policy):
    if namespace in policy['exceptions']:
        return 'privileged'
    if namespace in policy['restricted']:
        return 'restricted'
    return policy['default']


def labels(namespace, policy):
    return {f'pod-security.kubernetes.io/{mode}{suffix}': value
            for mode in ['enforce', 'audit', 'warn']
            for suffix, value in [('', level(namespace, policy) if mode == 'enforce' else 'restricted'),
                                  ('-version', policy['version'])]}


def templates(objects):
    for obj in objects:
        kind = obj['kind']
        if kind == 'Pod':
            if obj.get('status', {}).get('phase') not in ['Running', 'Pending']:
                continue
            template = obj
        elif kind == 'CronJob':
            template = obj['spec']['jobTemplate']['spec']['template']
        elif kind == 'PreBackupPod':
            template = obj['spec']['pod']
        else:
            template = obj['spec'].get('template')
        if template and template.get('spec', {}).get('containers'):
            template = copy.deepcopy(template)
            if kind == 'StatefulSet':
                volumes = template['spec'].setdefault('volumes', [])
                existing = {v['name'] for v in volumes}
                for claim in obj['spec'].get('volumeClaimTemplates', []):
                    name = claim['metadata']['name']
                    if name not in existing:
                        volumes.append({'name': name, 'persistentVolumeClaim': {'claimName': 'admission-check-' + name}})
            yield obj['metadata']['namespace'], kind + '/' + obj['metadata']['name'], template


def audit_pod(template, namespace):
    spec = copy.deepcopy(template['spec'])
    # Admission checks security fields, not the availability of the app's SA.
    # No workload is persisted, scheduled, started or attached to storage.
    spec['serviceAccountName'] = 'default'
    spec.pop('serviceAccount', None)
    spec.pop('ephemeralContainers', None)
    # Persisted Pods have matchLabelKeys expanded into selectors by admission;
    # resubmitting that scheduling-only field is invalid, independent of PSA.
    spec.pop('topologySpreadConstraints', None)
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'generateName': 'admission-check-', 'namespace': namespace,
                         'annotations': copy.deepcopy(template.get('metadata', {}).get('annotations', {})),
                         'labels': copy.deepcopy(template.get('metadata', {}).get('labels', {}))}, 'spec': spec}


def preflight(objects, namespaces, policy):
    scratch = {}
    failures = []
    try:
        for profile in ['baseline', 'restricted']:
            name = 'psa-check-' + profile + '-' + uuid.uuid4().hex[:8]
            result = kubectl('create', '-f', '-', body={'apiVersion': 'v1', 'kind': 'Namespace',
                'metadata': {'name': name, 'labels': {
                    'pod-security.kubernetes.io/enforce': profile,
                    'pod-security.kubernetes.io/enforce-version': policy['version']}}})
            if result.returncode: raise RuntimeError(result.stderr)
            scratch[profile] = name

        candidates = [(ns, name, template) for ns, name, template in templates(objects)
                      if level(ns, policy) != 'privileged']
        def check(candidate):
            ns, name, template = candidate
            result = kubectl('create', '--dry-run=server', '-f', '-', '-o', 'name',
                             body=audit_pod(template, scratch[level(ns, policy)]))
            if result.returncode:
                # Do not print pod specs: they can contain inline credentials.
                return f'{ns}/{name}: {result.stderr.strip()}'
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            failures.extend(result for result in pool.map(check, candidates) if result)
        print(f'Checked {len(candidates)} current pods and controller templates without creating pods.')
        if failures:
            raise RuntimeError('\n'.join(failures))
    finally:
        for name in scratch.values():
            result = kubectl('delete', 'namespace', name, '--wait=false')
            if result.returncode: print('Cleanup required:', name)


def verify_namespace(namespace, policy):
    # Prove the admission decision rather than merely checking labels.
    spec = {'containers': [{'name': 'check', 'image': 'busybox:1.37',
                           'securityContext': {'privileged': True}}]}
    result = kubectl('create', '--dry-run=server', '-f', '-', body={
        'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
            'generateName': 'denial-check-', 'namespace': namespace}, 'spec': spec})
    if result.returncode == 0 or 'violates PodSecurity' not in result.stderr:
        raise RuntimeError(f'{namespace}: expected PodSecurity denial')
    if level(namespace, policy) == 'restricted':
        spec['containers'][0]['securityContext'] = {}
        result = kubectl('create', '--dry-run=server', '-f', '-', body={
            'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
                'generateName': 'restricted-check-', 'namespace': namespace}, 'spec': spec})
        if result.returncode == 0 or 'violates PodSecurity' not in result.stderr:
            raise RuntimeError(f'{namespace}: expected restricted-profile denial')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    policy = json.loads((ROOT / 'policy.json').read_text())
    namespace_objects = [n for n in get('namespaces') if not n['metadata'].get('deletionTimestamp')]
    namespaces = [n['metadata']['name'] for n in namespace_objects]
    rank = {'privileged': 0, 'baseline': 1, 'restricted': 2}
    for n in namespace_objects:
        ns = n['metadata']['name']
        current = n['metadata'].get('labels', {}).get('pod-security.kubernetes.io/enforce', 'privileged')
        if rank.get(current, 0) > rank[level(ns, policy)]:
            raise RuntimeError(f'{ns}: refusing to weaken existing {current} enforcement')
    objects = get('deployments,statefulsets,daemonsets,cronjobs,jobs,pods')
    objects += get('autoscalingrunnersets.actions.github.com')
    objects += get('prebackuppods.k8up.io')
    for ns in namespaces:
        print(f'{ns}: {level(ns, policy)}' + (f" — {policy['exceptions'][ns]}" if ns in policy['exceptions'] else ''))
    preflight(objects, namespaces, policy)
    if not args.apply:
        print('Preflight passed. Use --apply to enforce these labels.')
        return
    for ns in namespaces:
        result = kubectl('label', 'namespace', ns, '--overwrite',
                         *(f'{key}={value}' for key, value in labels(ns, policy).items()))
        if result.returncode: raise RuntimeError(result.stderr)
        # Existing pods are not evicted by namespace labels. Reconcile only after
        # all future controller templates have passed the server-side dry run.
        if level(ns, policy) != 'privileged': verify_namespace(ns, policy)
        print(ns, 'enforced and verified' if level(ns, policy) != 'privileged' else 'explicit exception recorded')


if __name__ == '__main__':
    main()
