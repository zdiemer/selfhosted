#!/usr/bin/env python3
"""Install on the primary host; secret travels only in memory and SSH stdin."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    host = 'root@zachd-ubuntu'
    if not args.apply:
        print('Will install and enable the expiry timer on ' + host)
        print('Use --apply to copy code, resolve ntfy-seed/pw-alerts, and start monitoring.')
        return
    secret = json.loads(subprocess.check_output(
        ['kubectl', '-n', 'infra', 'get', 'secret', 'ntfy-seed', '-o', 'json']))
    password = base64.b64decode(secret['data']['pw-alerts'])
    if not password.strip():
        raise ValueError('Empty publisher password')
    files = {
        '/usr/local/lib/selfhosted-tailscale-expiry/monitor.py': (HERE / 'monitor.py').read_bytes(),
        '/etc/selfhosted/tailscale-expiry/ntfy-password': password,
    }
    for name in ['selfhosted-tailscale-expiry.service', 'selfhosted-tailscale-expiry.timer']:
        files['/etc/systemd/system/' + name] = (HERE / name).read_bytes()
    # Fixed paths and base64 payloads; shell tracing is explicitly off.
    script = 'set +x\nset -eu\numask 077\n'
    script += 'install -d -m 0755 /usr/local/lib/selfhosted-tailscale-expiry\n'
    script += 'install -d -m 0700 /etc/selfhosted/tailscale-expiry\n'
    for path, data in files.items():
        mode = '0600' if path.endswith('ntfy-password') else '0644'
        quoted = shlex.quote(path)
        script += f"printf %s '{base64.b64encode(data).decode()}' | base64 -d > {quoted}.new\n"
        script += f'chmod {mode} {quoted}.new\nmv {quoted}.new {quoted}\n'
    script += '''systemd-analyze verify /etc/systemd/system/selfhosted-tailscale-expiry.service /etc/systemd/system/selfhosted-tailscale-expiry.timer
systemctl daemon-reload
systemctl enable --now selfhosted-tailscale-expiry.timer
systemctl start selfhosted-tailscale-expiry.service
systemctl show selfhosted-tailscale-expiry.service -p Result -p ExecMainStatus
systemctl list-timers selfhosted-tailscale-expiry.timer --no-pager
'''
    subprocess.run(['tailscale', 'ssh', host, 'bash -s'], input=script.encode(), check=True)


if __name__ == '__main__':
    main()
