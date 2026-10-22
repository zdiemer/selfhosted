#!/usr/bin/env python3
"""Read-only database validation on a quarantined DO backup clone, never production."""
import argparse
import ipaddress
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.request

PRODUCTION_IPS = {'64.227.0.147', '147.182.206.23'}
PRODUCTION_IDS = {'587384488'}  # Whatnow, verified against live metadata.


def guard(expected_id, expected_ip, metadata_id, metadata_ip, hostname):
    if not expected_id.isdigit() or expected_id in PRODUCTION_IDS:
        raise ValueError('Expected ID must identify a new rehearsal droplet')
    address = ipaddress.ip_address(expected_ip)
    if not address.is_global or expected_ip in PRODUCTION_IPS:
        raise ValueError('Expected IP must be a new public rehearsal address')
    if metadata_id != expected_id or metadata_ip != expected_ip:
        raise ValueError('Provider metadata does not match the explicitly selected clone')
    if not hostname.startswith('dr-rehearsal-'):
        raise ValueError('Provider hostname must start dr-rehearsal-')


def validate_sqlite(path):
    if not path.is_file():
        raise ValueError('Required restored database is absent')
    # Do not use immutable=1: a crash-consistent backup may require its WAL.
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as database:
        integrity = database.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise ValueError('SQLite integrity check failed (details deliberately not logged)')
        if database.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('SQLite foreign-key check failed')
        tables = [row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if not tables:
            raise ValueError('No application tables restored')
        rows = sum(database.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                   for name in tables)
        if rows < 1:
            raise ValueError('No application rows restored')
    return {'database': path.name, 'tables': len(tables), 'rows': rows, 'integrity': 'ok'}


def metadata(field):
    # Bypass any inherited HTTP proxy. Never fetch provider user-data or secrets.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://169.254.169.254/metadata/v1/' + field, timeout=5) as response:
        return response.read().decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['whatnow', 'egress'])
    parser.add_argument('--droplet-id', required=True)
    parser.add_argument('--public-ip', required=True)
    args = parser.parse_args()
    guard(args.droplet_id, args.public_ip, metadata('id'),
          metadata('interfaces/public/0/ipv4/address'), metadata('hostname'))
    services = ['docker', 'containerd', 'tailscaled', 'crowdsec', 'crowdsec-firewall-bouncer',
                'caddy', 'haproxy', 'squid', 'coturn', 'cron']
    for service in services:
        result = subprocess.run(['systemctl', 'show', service, '--property=ActiveState', '--value'],
                                capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip() not in {'inactive', 'failed'}:
            raise SystemExit('Service must be stopped before verification: ' + service)
    # Docker live-restore can leave containers alive after the daemon stops.
    running = subprocess.check_output(['ps', '-e', '-o', 'comm='], text=True).splitlines()
    if any(name.strip() in {'containerd-shim', 'containerd-shim-runc-v2', 'docker-proxy'} or
           name.strip().startswith('containerd-shim') for name in running):
        raise SystemExit('Container processes remain; stop them on the clone before validation')
    paths = ([Path('/opt/whatnowgg/data/users.sqlite'), Path('/opt/whatnowgg/data/enrichment.sqlite')]
             if args.target == 'whatnow' else [Path('/var/lib/crowdsec/data/crowdsec.db')])
    for path in paths:
        print(json.dumps(validate_sqlite(path)))
    print('CLONE_DATABASES_VERIFIED (runtime and reboot rehearsal still required)')


if __name__ == '__main__':
    main()
