"""Bounded, credential-free endpoint checks, including external SSH listeners."""
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

_CACHE = {}


def probe(check, publish_urls=True):
    key = json.dumps(check, sort_keys=True)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < float(check.get('intervalSeconds', 0)):
        result = dict(cached[1])
    else:
        result = {'name': check.get('name', 'service'), 'ok': False,
                  'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        timeout = min(5, max(0.1, float(check.get('timeoutSeconds', 3))))
        try:
            target = urllib.parse.urlsplit(check['url'])
            if target.scheme == 'ssh':
                with socket.create_connection((target.hostname, target.port or 22), timeout) as sock:
                    sock.settimeout(timeout)
                    banner = sock.recv(255)
                result['ok'] = banner.startswith(b'SSH-')
                result['detail'] = 'SSH banner received' if result['ok'] else 'Invalid SSH banner'
            elif target.scheme in ('http', 'https'):
                request = urllib.request.Request(check['url'], headers={'Cache-Control': 'no-cache', 'User-Agent': 'cluster-status/1.0'})
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        code = response.status
                except urllib.error.HTTPError as error:
                    code = error.code
                    error.close()
                result.update(status=code, ok=code == int(check.get('expectStatus', 200)))
            else:
                raise ValueError('unsupported probe scheme')
        except Exception as error:
            result['reason'] = type(error).__name__
        result['latencyMs'] = round((time.monotonic() - now) * 1000)
        _CACHE[key] = (now, dict(result))
    if publish_urls:
        result['url'] = check['url']
    return result


def _safe_probe(check, publish_urls):
    try:
        return probe(check, publish_urls)
    except Exception as error:
        # A malformed entry must not prevent other checks or cluster collection.
        return {'name': check.get('name', 'service'), 'ok': False,
                'reason': type(error).__name__}


def run_checks(checks, publish_urls=True):
    # Independent failures cannot turn N short timeouts into a serial stall.
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda check: _safe_probe(check, publish_urls),
                             [check for check in checks if check.get('url')]))
