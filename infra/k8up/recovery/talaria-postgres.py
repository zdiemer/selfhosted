#!/usr/bin/env python3
"""Create an isolated, non-networked PostgreSQL restore drill from an explicit snapshot."""
import argparse
import json
import re
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('snapshot', help='full restic snapshot ID from talaria-dump.pgdump')
p.add_argument('--archive', choices=['/talaria-dump.pgdump', '/dump/talaria-dump.pgdump'], default='/dump/talaria-dump.pgdump')
p.add_argument('--name', default='talaria-postgres-recovery')
p.add_argument('--apply', action='store_true')
a = p.parse_args()
if not re.fullmatch('[a-f0-9]{64}', a.snapshot) or not re.fullmatch('[a-z][a-z0-9-]{0,45}', a.name):
    p.error('Use a full snapshot ID and a DNS-safe job name')
labels = {'app.kubernetes.io/name': 'postgres-recovery', 'recovery-drill': a.name}
meta = {'name': a.name, 'namespace': 'talaria', 'labels': labels}
security = {'allowPrivilegeEscalation': False, 'capabilities': {'drop':['ALL']}, 'readOnlyRootFilesystem': True}
script = r'''set -eu
export PGDATA=/work/pgdata PGHOST=/tmp PGUSER=postgres
initdb -U postgres --auth-local=trust --auth-host=reject >/tmp/init.log
pg_ctl -l /tmp/postgres.log -o "-c listen_addresses='' -c unix_socket_directories=/tmp -c max_wal_size=4GB -c shared_buffers=256MB" start
trap 'pg_ctl -m fast stop >/dev/null' EXIT
createdb recovery
pg_restore --list /work/talaria-dump.pgdump >/work/archive.list
printf 'Archive entries: '; wc -l </work/archive.list
pg_restore --exit-on-error --no-owner --no-acl --jobs=4 -d recovery /work/talaria-dump.pgdump
psql -X -v ON_ERROR_STOP=1 -d recovery -c "SELECT count(*) AS restored_tables FROM pg_tables WHERE schemaname='public';"
# Refresh table statistics and check restored index validity.
psql -X -v ON_ERROR_STOP=1 -d recovery -c 'ANALYZE;'
psql -X -v ON_ERROR_STOP=1 -d recovery -c "DO \\$\\$ BEGIN IF EXISTS (SELECT 1 FROM pg_index WHERE NOT indisvalid) THEN RAISE EXCEPTION 'invalid restored indexes'; END IF; END \\$\\$;"
psql -X -v ON_ERROR_STOP=1 -d recovery -c "SELECT count(*) AS restored_auction_items FROM auction_items;"
psql -X -v ON_ERROR_STOP=1 -d recovery -c "SELECT pg_size_pretty(pg_database_size('recovery')) AS restored_size;"
echo RESTORE_VERIFIED
'''
archive_local = '/work' + a.archive
script = script.replace('/work/talaria-dump.pgdump', archive_local)
objects = [
    {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':meta,'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'truenas-iscsi','resources':{'requests':{'storage':'400Gi'}}}},
    {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':meta,'spec':{'podSelector':{'matchLabels':labels},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':[]}},
    {'apiVersion':'batch/v1','kind':'Job','metadata':meta,'spec':{'backoffLimit':0,'activeDeadlineSeconds':21600,'template':{'metadata':{'labels':labels},'spec':{
        'automountServiceAccountToken':False,'restartPolicy':'Never',
        'securityContext':{'fsGroup':70,'seccompProfile':{'type':'RuntimeDefault'}},
        'initContainers':[{'name':'restore-archive','image':'restic/restic:0.19.1',
            'command':['/bin/sh','-ec',f'restic --no-lock --no-cache restore {a.snapshot} --target /work --include {a.archive}; chown 70:70 {archive_local}; chmod 600 {archive_local}'],
            'env':[{'name':'RESTIC_REPOSITORY','value':'/repo'},{'name':'RESTIC_PASSWORD_FILE','value':'/credentials/password'}],
            'securityContext':{**security,'capabilities':{'drop':['ALL'],'add':['CHOWN','FOWNER','DAC_READ_SEARCH']}},
            'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
            'volumeMounts':[{'name':'work','mountPath':'/work'},{'name':'repo','mountPath':'/repo','readOnly':True},{'name':'password','mountPath':'/credentials','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],
        'containers':[{'name':'postgres','image':'postgres:15.19-alpine','command':['sh','-ec',script],
            'securityContext':{**security,'runAsUser':70,'runAsGroup':70,'runAsNonRoot':True},
            'resources':{'requests':{'cpu':'500m','memory':'1Gi'},'limits':{'cpu':'4','memory':'4Gi'}},
            'volumeMounts':[{'name':'work','mountPath':'/work'},{'name':'tmp','mountPath':'/tmp'}]}],
        'volumes':[{'name':'work','persistentVolumeClaim':{'claimName':a.name}},
                   {'name':'repo','persistentVolumeClaim':{'claimName':'k8up-restic-repo','readOnly':True}},
                   {'name':'password','secret':{'secretName':'k8up-restic-password','defaultMode':0o400}},
                   {'name':'tmp','emptyDir':{}}]
    }}}}
]
if a.apply:
    # Never reset/reuse a drill's database volume; a second run needs a new name.
    found = subprocess.run(['kubectl','get','pvc',a.name,'-n','talaria','--ignore-not-found','-o','name'],check=True,capture_output=True,text=True)
    if found.stdout.strip():
        raise SystemExit('Scratch PVC already exists; inspect it and use a new drill name')
    subprocess.run(['kubectl','create','-f','-'],input=json.dumps({'apiVersion':'v1','kind':'List','items':objects}).encode(),check=True)
else:
    print(json.dumps({'apiVersion':'v1','kind':'List','items':objects},indent=2))
