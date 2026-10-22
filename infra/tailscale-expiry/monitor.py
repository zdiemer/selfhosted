#!/usr/bin/env python3
"""Read public node-expiry metadata; publish only after entering a new window."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

WINDOWS = [(7 * 86400, '7d'), (3 * 86400, '3d'), (2 * 86400, '2d'),
           (86400, '1d'), (43200, '12hr'), (0, 'expired')]


def plan(status, state, now):
    if status.get('BackendState') != 'Running' or status.get('Health'):
        raise RuntimeError('Tailscale is not healthy; refusing a possibly stale inventory')
    devices = [status['Self'], *status.get('Peer', {}).values()]
    if not devices or not all(p.get('ID') for p in devices):
        raise ValueError('Missing stable device identity')
    updated, notices = dict(state), []
    for peer in devices:
        identity, expiry = peer['ID'], peer.get('KeyExpiry')
        if not expiry or expiry.startswith('0001-'):
            # Tailscale omits KeyExpiry when expiry is disabled.
            updated.pop(identity, None)
            continue
        deadline = datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp()
        remaining = deadline - now
        stage = max((i for i, (seconds, _) in enumerate(WINDOWS) if remaining <= seconds), default=-1)
        previous = state.get(identity, {})
        sent = previous.get('stage', -1) if previous.get('expiry') == expiry else -1
        # Keep the most urgent delivered stage even if the wall clock goes back.
        updated[identity] = {'expiry': expiry, 'stage': max(stage, sent)}
        if stage > sent:
            name = peer.get('HostName') or peer.get('DNSName') or identity
            ip = next(iter(peer.get('TailscaleIPs', [])), identity)
            label = WINDOWS[stage][1]
            notices.append(f'{name} ({ip}): {label} window; expires {expiry}')
    return updated, notices


def save(path, state):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish(lines, title="Tailscale key expiration"):
    password = (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'ntfy-password').read_text().strip()
    auth = base64.b64encode(('alerts:' + password).encode()).decode()
    body = (title + ':\n\n' + '\n'.join(lines)
            + '\n\nReview https://login.tailscale.com/admin/machines . '
              'Reauthenticate using a local console or alternate access path.')
    req = urllib.request.Request(os.environ['NTFY_URL'], data=body.encode(), headers={
        'Authorization': 'Basic ' + auth, 'Title': title,
        'Priority': 'high', 'Tags': 'warning',
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        receipt = json.load(response)
        if response.status != 200 or receipt.get('event') != 'message' or not receipt.get('id'):
            raise RuntimeError('ntfy did not acknowledge the notification')


def check(status, state, now, send, persist):
    updated, notices = plan(status, state, now)
    if notices:
        send(notices)
    # Never suppress a retry when ntfy failed. A crash after delivery but before
    # persistence can cause a duplicate: delivery is at-least-once, not exactly-once.
    persist(updated)
    return len(notices)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    state_dir = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/selfhosted-tailscale-expiry'))
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failure_path = state_dir / 'failure.json'
        try:
            run_check(args, state_dir)
        except Exception:
            # Also surface local inventory/parse failures, at most once per day.
            # If ntfy itself is down, the systemd failure remains and the timer retries.
            try:
                now = datetime.now(timezone.utc).timestamp()
                last = json.loads(failure_path.read_text()) if failure_path.exists() else {}
                if not args.dry_run and now - last.get('notified', 0) >= 86400:
                    publish(['Expiry monitoring failed on zachd-ubuntu. Check journalctl '
                             '-u selfhosted-tailscale-expiry.service; reminders may be delayed.'],
                            title='Tailscale expiry monitor failed')
                    save(failure_path, {'notified': now})
            except Exception:
                print('Monitor failure notification could not be delivered', flush=True)
            raise
        else:
            if not args.dry_run:
                failure_path.unlink(missing_ok=True)


def run_check(args, state_dir):
    status = json.loads(subprocess.check_output(['/usr/bin/tailscale', 'status', '--json'], timeout=20))
    path = state_dir / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    count = check(status, state, datetime.now(timezone.utc).timestamp(),
                  (lambda lines: print('\n'.join(lines))) if args.dry_run else publish,
                  (lambda _: None) if args.dry_run else (lambda s: save(path, s)))
    print(f'devices={1 + len(status.get("Peer", {}))} due={count} dry_run={args.dry_run}', flush=True)


if __name__ == '__main__':
    main()
