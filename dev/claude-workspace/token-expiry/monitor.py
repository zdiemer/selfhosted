"""Warn, over private ntfy, before the workspace's Claude login needs a human.

TWO EXPIRIES LIVE IN ~/.claude/.credentials.json AND ONLY ONE MATTERS HERE
`claudeAiOauth.expiresAt` is the access token: it lapses every few hours and the
CLI silently exchanges the refresh token for a new one, so alerting on it would
page daily about something that fixes itself. `refreshTokenExpiresAt` is the one
that ends in a human opening a browser — when it passes, every surface on this
pod (the /term shell, the Happy app, the Signal/WhatsApp gateway, the scheduled
trading agent) fails with an auth error at once, and the only repair is the
interactive `claude` login described in README "Signing in". That is what this
watches.

NO STATE, BY DESIGN
The CronJob runs once a day, and a run inside the warning window publishes one
notification. That makes the reminder cadence the schedule itself rather than a
debounce ConfigMap: there is nothing to reconcile, and a missed Job costs one
reminder out of the warning window's worth instead of resetting a cooldown. The
home PVC is mounted read-only here for the same reason — a monitor has no
business writing to the volume that holds the credentials it reads.
"""
import base64
import json
import os
import time
import urllib.request
from pathlib import Path

DAY_MS = 86_400_000


def assess(raw, now_ms, warn_days):
    """Return (notification, title, priority, tags) — notification None = silent.

    An unreadable file, a missing `claudeAiOauth` block and a missing expiry
    field are all reported rather than swallowed. The first two mean the pod is
    already signed out; the third means this check cannot see the thing it
    exists to watch, and a blind monitor that stays quiet is worse than a rare
    false alarm.
    """
    if raw is None:
        return ('The workspace pod has no ~/.claude/.credentials.json, so '
                'claude is signed out. Open /term and run `claude` to sign in '
                'again (README "Signing in").',
                'Claude workspace is signed out', 'high', 'warning')
    try:
        oauth = json.loads(raw)['claudeAiOauth']
    except (ValueError, KeyError, TypeError):
        return ('~/.claude/.credentials.json on the workspace pod has no '
                'readable claudeAiOauth block. Treat the pod as signed out and '
                'run `claude` in /term.',
                'Claude workspace is signed out', 'high', 'warning')
    expires_at = oauth.get('refreshTokenExpiresAt')
    if not isinstance(expires_at, int):
        return ('This credentials file carries no refreshTokenExpiresAt, so the '
                'expiry of the workspace login cannot be checked. The CLI may '
                'have changed the file format; see '
                'dev/claude-workspace/token-expiry/monitor.py.',
                'Claude token expiry unknown', 'default', 'mag')

    remaining_ms = expires_at - now_ms
    # Floor, so "0 days left" means today and never tomorrow. A refresh token
    # 23 hours out must not round up into the silent branch.
    days = remaining_ms // DAY_MS
    if remaining_ms > 0 and days >= warn_days:
        return (None, None, None, None)

    when = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at / 1000))
    if remaining_ms <= 0:
        return (f'The Claude OAuth refresh token expired at {when}. Every '
                'surface on the workspace pod (term, Happy, the messaging '
                'gateway, scheduled agents) is signed out until someone runs '
                '`claude` in /term and completes the browser login.',
                'Claude workspace login has expired', 'urgent', 'rotating_light')
    plural = '' if days == 1 else 's'
    priority = 'high' if days <= 1 else 'default'
    return (f'The Claude OAuth refresh token for the workspace pod expires in '
            f'{days} day{plural}, at {when}. Renew it before then: open /term, '
            'run `claude`, and complete the browser login — the token refreshes '
            'itself until this date but cannot renew past it.',
            'Claude workspace token expires soon', priority, 'hourglass')


def publish(url, password, body, title, priority, tags):
    auth = base64.b64encode(('alerts:' + password).encode()).decode()
    request = urllib.request.Request(url, data=body.encode(), headers={
        'Authorization': 'Basic ' + auth,
        'Title': title,
        'Priority': priority,
        'Tags': tags,
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        receipt = json.load(response)
    if not receipt.get('id') or receipt.get('event') != 'message':
        raise RuntimeError('ntfy did not acknowledge notification')


def main():
    path = Path(os.environ.get('CREDENTIALS_FILE',
                               '/home/node/.claude/.credentials.json'))
    try:
        raw = path.read_text()
    except OSError:
        raw = None

    warn_days = int(os.environ.get('WARN_DAYS', '7'))
    body, title, priority, tags = assess(raw, time.time() * 1000, warn_days)
    if body is None:
        print('ok: more than %d days of login left' % warn_days, flush=True)
        return
    # Fail the Job if ntfy refuses: an unpublished warning is the one outcome
    # that must not look like a clean run.
    publish(os.environ['NTFY_URL'], os.environ['NTFY_PASSWORD'],
            body, title, priority, tags)
    print('notified: ' + title, flush=True)


if __name__ == '__main__':
    main()
