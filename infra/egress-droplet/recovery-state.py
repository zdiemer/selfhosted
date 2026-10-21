#!/usr/bin/env python3
"""Vault-backed egress identity recovery; Tailscale state is deliberately excluded."""
import argparse
import datetime
import hashlib
import io
from pathlib import PurePosixPath
import shlex
import subprocess
import tarfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('action', choices=['backup', 'verify', 'restore'])
p.add_argument('--host', default='egress-sfo3')
p.add_argument('--document', default='egress-sfo3-recovery-' + datetime.datetime.now(datetime.UTC).strftime('%Y%m%d'))
a = p.parse_args()
if a.host.startswith('-'):
    p.error('Invalid SSH host')


def run(args, data=None):
    return subprocess.run(args, input=data, check=True, stdout=subprocess.PIPE).stdout


def ssh(code, data=None):
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', a.host,
                'python3 -c ' + shlex.quote(code)], data)


backup_code = '''
import io, os, pathlib, sqlite3, sys, tarfile, tempfile
with tempfile.TemporaryDirectory(prefix='egress-recovery-', dir='/run') as tmp:
    os.chmod(tmp, 0o700)
    path=pathlib.Path(tmp)/'crowdsec.db'
    with sqlite3.connect('file:/var/lib/crowdsec/data/crowdsec.db?mode=ro', uri=True) as source:
        with sqlite3.connect(path) as dest:
            source.backup(dest)
            assert dest.execute('pragma integrity_check').fetchone()[0] == 'ok'
    out=io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        for name in ['/etc/crowdsec', '/var/lib/caddy/.local/share/caddy']:
            tar.add(name, arcname=name.lstrip('/'))
        info=tar.gettarinfo('/var/lib/crowdsec/data/crowdsec.db', arcname='var/lib/crowdsec/data/crowdsec.db')
        info.size=path.stat().st_size
        with path.open('rb') as db:
            tar.addfile(info, db)
    sys.stdout.buffer.write(out.getvalue())
'''


def validate(data):
    allowed = ('etc/crowdsec', 'var/lib/crowdsec/data/crowdsec.db', 'var/lib/caddy/.local/share/caddy')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        names = set()
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not any(member.name == prefix or member.name.startswith(prefix + '/') for prefix in allowed):
                raise ValueError('Unexpected archive destination')
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError('Unexpected archive member type')
            if member.issym():
                # CrowdSec uses absolute links into its own hub, which is archived.
                if not member.linkname.startswith('/etc/crowdsec/') or '..' in PurePosixPath(member.linkname).parts:
                    raise ValueError('Unexpected archive symlink')
            names.add(member.name)
        if 'var/lib/crowdsec/data/crowdsec.db' not in names or 'etc/crowdsec/online_api_credentials.yaml' not in names:
            raise ValueError('Incomplete CrowdSec identity archive')
    return len(names)


if a.action == 'backup':
    # Existence check distinguishes auth failures from a missing item.
    import json
    items = json.loads(run(['op','item','list','--vault','homelab','--format=json']))
    if any(i['title'] == a.document for i in items):
        raise SystemExit('Document already exists; use a new dated name or verify it')
    data = ssh(backup_code)
    count = validate(data)
    run(['op','document','create','-','--vault','homelab','--title',a.document,'--file-name','egress-recovery.tar.gz'], data)
    saved = run(['op','document','get',a.document,'--vault','homelab'])
    assert hashlib.sha256(saved).digest() == hashlib.sha256(data).digest()
    print(f'Vault recovery archive verified: {count} entries, {len(data)} bytes; {a.document}')
else:
    data = run(['op','document','get',a.document,'--vault','homelab'])
    count = validate(data)
    if a.action == 'restore':
        # Only a prepared replacement is eligible, never overwrite an adopted live host.
        restore_code = '''
import io, pathlib, subprocess, sys, tarfile
assert not pathlib.Path('/etc/egress-iac-managed').exists(), 'Refusing an adopted live host'
assert pathlib.Path('/etc/egress-bootstrap.nft').exists(), 'Run prepare.sh first'
for name in ['crowdsec', 'crowdsec-firewall-bouncer', 'caddy']:
    subprocess.run(['systemctl', 'stop', name], check=True)
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode='r:gz') as archive:
    archive.extractall('/', numeric_owner=False)
pathlib.Path('/etc/egress-state-restored').write_text('Vault recovery state restored; apply desired configuration next.\\n')
'''
        ssh(restore_code, data)
    print(f'{a.action}: {count} validated archive entries; no Tailscale identity included')
