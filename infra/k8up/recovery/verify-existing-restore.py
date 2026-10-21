#!/usr/bin/env python3
"""Finish validation of a scratch restore stopped by the legacy SQL quoting bug.

Refuses running Jobs, failed restores, production volumes, or other failures.
Never initializes a database or replays the archive; never mounts credentials.
"""
import argparse
import copy
import json
import re
import subprocess


def kubectl(*args):
    return subprocess.check_output(['kubectl', *args], text=True)


def build_validation(job, logs, name):
    status = job.get('status', {})
    if not any(c['type'] == 'Failed' and c['status'] == 'True' for c in status.get('conditions', [])):
        raise ValueError('Original restore must have terminated with the known validation failure')
    template = job['spec']['template']
    labels = template['metadata']['labels']
    original = job['metadata']['name']
    if labels.get('app.kubernetes.io/name') != 'postgres-recovery' or labels.get('recovery-drill') != original:
        raise ValueError('Not an isolated PostgreSQL recovery drill')
    spec = copy.deepcopy(template['spec'])
    container = next(c for c in spec['containers'] if c['name'] == 'postgres')
    command = container['command'][-1]
    # These markers are emitted only after pg_restore --exit-on-error succeeds.
    if not all(marker in logs for marker in ['restored_tables', 'ANALYZE', 'syntax error']):
        raise ValueError('Archive replay/ANALYZE success and SQL syntax failure are not established')
    if r'DO \\$\\$' not in command or 'pg_restore --exit-on-error' not in command:
        raise ValueError('Original Job does not contain the known legacy quoting bug')
    work = next(v for v in spec['volumes'] if v['name'] == 'work')
    if work.get('persistentVolumeClaim', {}).get('claimName') != original:
        raise ValueError('Scratch claim must match the recovery Job name')
    container['command'] = ['sh', '-ec', r'''set -eu
export PGDATA=/work/pgdata PGHOST=/tmp PGUSER=postgres
test -s "$PGDATA/PG_VERSION"
# A new pod's fsGroup volume setup can widen this scratch directory mode.
chmod 0700 "$PGDATA"
pg_ctl -l /tmp/postgres.log -o "-c listen_addresses='' -c unix_socket_directories=/tmp" start || { cat /tmp/postgres.log; exit 1; }
trap 'pg_ctl -m fast stop >/dev/null' EXIT
psql -X -v ON_ERROR_STOP=1 -d recovery <<'SQL'
-- Division by zero aborts psql if any invalid object remains. Avoid dollar
-- quoting: Kubernetes expands paired dollar signs in container commands.
SELECT 1 / (count(*) = 0)::int AS indexes_valid FROM pg_index WHERE NOT indisvalid;
SELECT 1 / (count(*) = 0)::int AS constraints_valid FROM pg_constraint WHERE NOT convalidated;
SELECT count(*) AS restored_tables FROM pg_tables WHERE schemaname='public';
SELECT count(*) AS restored_auction_items FROM auction_items;
SELECT pg_size_pretty(pg_database_size('recovery')) AS restored_size;
SQL
echo RESTORE_VERIFIED
''']
    # Keep only the non-root database container and its scratch/tmp mounts.
    spec['containers'] = [container]
    spec.pop('initContainers', None)
    spec['volumes'] = [work, {'name': 'tmp', 'emptyDir': {}}]
    spec['automountServiceAccountToken'] = False
    meta = {'name': name, 'namespace': 'talaria'}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta,
              'spec': {'podSelector': {'matchLabels': {'recovery-drill': name}},
                       'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    result = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': meta,
              'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 3600,
                       'template': {'metadata': {'labels': {'app.kubernetes.io/name': 'postgres-recovery', 'recovery-drill': name}}, 'spec': spec}}}
    return [policy, result]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job')
    parser.add_argument('--name', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not all(re.fullmatch('[a-z][a-z0-9-]{0,62}', s) for s in [args.job, args.name]):
        parser.error('Use DNS-safe Job names')
    job = json.loads(kubectl('get', 'job', args.job, '-n', 'talaria', '-o', 'json'))
    logs = kubectl('logs', '-n', 'talaria', 'job/' + args.job, '-c', 'postgres')
    items = build_validation(job, logs, args.name)
    if args.apply:
        for item in items:
            subprocess.run(['kubectl', 'create', '-f', '-'], input=json.dumps(item), text=True, check=True)
    else:
        print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}, indent=2))


if __name__ == '__main__':
    main()
