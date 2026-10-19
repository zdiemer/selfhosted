"""Probe the SMS dependency and publish transitions to private ntfy.

Runs once per minute as a Forbid-concurrency CronJob. State survives Job/pod
replacement in one ConfigMap. No phone credentials or message bodies are read.
"""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path


def transition(state, healthy, now):
    state = dict(state)
    if healthy:
        notification = 'recovered' if state.get('notified') else None
        return {}, notification
    state.setdefault('failed_since', now)
    due = now - state['failed_since'] >= 300
    repeat_due = now - state.get('last_notification', 0) >= 14400
    if due and (not state.get('notified') or repeat_due):
        state.update(notified=True, last_notification=now)
        return state, 'down'
    return state, None


def main():
    root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    namespace = (root / 'namespace').read_text().strip()
    token = (root / 'token').read_text().strip()
    context = ssl.create_default_context(cafile=str(root / 'ca.crt'))
    url = ('https://kubernetes.default.svc/api/v1/namespaces/' + namespace
           + '/configmaps/' + os.environ['STATE_CONFIGMAP'])

    def state_api(method, body=None):
        req = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, context=context, timeout=10) as response:
            return json.load(response)

    current = state_api('GET')
    previous = json.loads(current.get('data', {}).get('state', '{}'))
    try:
        with urllib.request.urlopen(os.environ['PROBE_URL'], timeout=5) as response:
            healthy = response.status == 200
    except (OSError, urllib.error.URLError):
        healthy = False
    state, notification = transition(previous, healthy, time.time())
    if notification:
        recovered = notification == 'recovered'
        body = ('SMS phone gateway connection restored. This confirms API connectivity, '
                'not carrier delivery; failed messages were not replayed.' if recovered else
                'SMS phone gateway has been unavailable for at least 5 minutes. '
                'Check the Android gateway app, Wi-Fi address, and local server. '
                'See docs/sms-gateway-recovery.md.')
        auth = base64.b64encode(('alerts:' + os.environ['NTFY_PASSWORD']).encode()).decode()
        req = urllib.request.Request(os.environ['NTFY_URL'], data=body.encode(), headers={
            'Authorization': 'Basic ' + auth,
            'Title': 'SMS gateway recovered' if recovered else 'SMS gateway unavailable',
            'Priority': 'default' if recovered else 'high',
            'Tags': 'white_check_mark' if recovered else 'warning',
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            receipt = json.load(response)
            if not receipt.get('id') or receipt.get('event') != 'message':
                raise RuntimeError('ntfy did not acknowledge notification')
        print('notification=' + notification, flush=True)
    # Persist only after ntfy accepts the message: failures remain retryable.
    # ResourceVersion detects concurrent updates; CronJob overlap is forbidden.
    current.setdefault('data', {})['state'] = json.dumps(state)
    state_api('PUT', current)
    print('gateway=' + ('up' if healthy else 'down'), flush=True)


if __name__ == '__main__':
    main()
