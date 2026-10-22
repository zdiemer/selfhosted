#!/usr/bin/env python3
"""Restore a stored application dump into an isolated, disposable database."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
TARGETS = {
    'paperless': ('docs', 'postgres', 'postgres:16.15-alpine', 70, '/docs-dump.pgdump', '15Gi'),
    'rachel': ('rachel', 'postgres', 'postgres:17.6-alpine', 70, '/rachel-dump.pgdump', '5Gi'),
    'romm': ('games', 'mariadb', 'mariadb:11.4.13', 999, '/games-dump.sql', '15Gi'),
    # Matches the live MongoDB 8 server image; the backup client uses database tools.
    'vocard': ('discord', 'mongo', 'mongo@sha256:e0ce8c35124d4a9f9785532d1f268f39e9728ffa1cb38f46fa482436424c4bd3', 999, '/discord-dump.archive.gz', '5Gi'),
}


def documents(target, snapshot, name):
    if not re.fullmatch('[a-f0-9]{64}', snapshot):
        raise ValueError('Use a full snapshot ID')
    if not re.fullmatch('db-restore-[a-z0-9-]{1,40}', name):
        raise ValueError('Name must start db-restore- and be DNS safe')
    namespace, engine, image, uid, archive, size = TARGETS[target]
    labels = {'recovery-drill': name}
    meta = {'name': name, 'namespace': namespace, 'labels': labels}
    security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}
    archive_local = '/work' + archive
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': meta,
           'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 7200,
                    'template': {'metadata': {'labels': labels}, 'spec': {
        'automountServiceAccountToken': False, 'restartPolicy': 'Never',
        'initContainers': [{
            'name': 'restore-archive', 'image': 'restic/restic:0.19.1',
            'command': ['sh', '-ec', f'restic --no-lock --no-cache restore {snapshot} --target /work --include {archive}; test -s {archive_local}; chown {uid}:{uid} /work {archive_local}; chmod 700 /work; chmod 600 {archive_local}'],
            'env': [{'name': 'RESTIC_REPOSITORY', 'value': '/repo'},
                    {'name': 'RESTIC_PASSWORD_FILE', 'value': '/credentials/password'},
                    {'name': 'GOMEMLIMIT', 'value': '512MiB'}],
            'securityContext': {**security, 'runAsUser': 0,
                                'capabilities': {'drop': ['ALL'], 'add': ['CHOWN', 'FOWNER', 'DAC_OVERRIDE']}},
            'resources': {'requests': {'cpu': '100m', 'memory': '512Mi'}, 'limits': {'cpu': '1', 'memory': '2Gi'}},
            'volumeMounts': [{'name': 'work', 'mountPath': '/work'},
                            {'name': 'repo', 'mountPath': '/repo', 'readOnly': True},
                            {'name': 'password', 'mountPath': '/credentials', 'readOnly': True},
                            {'name': 'tmp', 'mountPath': '/tmp'}],
        }],
        'containers': [{
            'name': 'database', 'image': image,
            'command': ['sh', '-ec', (ROOT / 'scripts' / (engine + '.sh')).read_text()],
            'env': [{'name': 'ARCHIVE', 'value': archive_local}, {'name': 'HOME', 'value': '/tmp'}],
            'securityContext': {**security, 'runAsUser': uid, 'runAsGroup': uid, 'runAsNonRoot': True},
            'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}},
            'volumeMounts': [{'name': 'work', 'mountPath': '/work'}, {'name': 'tmp', 'mountPath': '/tmp'}],
        }],
        'volumes': [{'name': 'work', 'persistentVolumeClaim': {'claimName': name}},
                    {'name': 'repo', 'persistentVolumeClaim': {'claimName': 'k8up-restic-repo', 'readOnly': True}},
                    {'name': 'password', 'secret': {'secretName': 'k8up-restic-password', 'defaultMode': 0o400}},
                    {'name': 'tmp', 'emptyDir': {}}],
    }}}}
    return [
        {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': meta,
         'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'truenas-iscsi', 'resources': {'requests': {'storage': size}}}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta,
         'spec': {'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}},
        job,
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=TARGETS)
    parser.add_argument('snapshot')
    parser.add_argument('--name', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    objects = documents(args.target, args.snapshot, args.name)
    if args.apply:
        namespace = TARGETS[args.target][0]
        for kind in ['pvc', 'job', 'networkpolicy']:
            existing = subprocess.check_output(['kubectl', '-n', namespace, 'get', kind, args.name, '--ignore-not-found', '-o', 'name'])
            if existing.strip():
                raise SystemExit('Refusing to reuse an existing drill resource: ' + kind)
        subprocess.run(['kubectl', 'create', '-f', '-'], input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}).encode(), check=True)
    else:
        print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}, indent=2))


if __name__ == '__main__':
    main()
