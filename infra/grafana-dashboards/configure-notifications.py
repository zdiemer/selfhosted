#!/usr/bin/env python3
"""Provision the public receiver; activate only after Cloudflare is reachable.

Run via configure-notifications.sh so the 1Password session is established.
Secrets are resolved in memory, never logged or passed on argv.
"""
import argparse
import json
from pathlib import Path
import subprocess
import urllib.request
import urllib.error
import yaml

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    cfg = json.loads((HERE / 'notifications.json').read_text())
    def secret(item):
        return yaml.safe_load(subprocess.check_output(['op', 'read', f'op://homelab/{item}/values.local.yaml']))
    grafana = secret('infra-grafana-dashboards')['grafana']
    relay = secret('infra-sms-relay')
    key = json.loads(relay['secrets']['apiKeys'])['grafana']
    def api(path, data=None, method=None):
        request = urllib.request.Request(grafana['url'].rstrip('/') + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={'Authorization':'Bearer '+grafana['token'], 'Content-Type':'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    if args.activate or cfg['enabled']:
        # Authenticated malformed payload must reach the app and return its
        # specific 400 response, without queuing a test notification.
        request = urllib.request.Request(cfg['url'], data=b'{}', headers={
            'Authorization': 'Bearer '+key, 'Content-Type':'application/json'})
        try:
            urllib.request.build_opener(NoRedirect).open(request, timeout=15)
            raise RuntimeError('Webhook accepted invalid payload; refusing activation')
        except urllib.error.HTTPError as error:
            if error.code != 400 or json.loads(error.read()).get('detail') != 'invalid Grafana webhook':
                raise RuntimeError('Public webhook is not ready; configure Cloudflare first') from None
    points = api('/api/v1/provisioning/contact-points')
    exists = any(p['uid'] == cfg['receiver'] for p in points)
    point = {'uid':cfg['receiver'], 'name':cfg['receiver'], 'type':'webhook',
             'disableResolveMessage':False, 'settings':{'url':cfg['url'], 'httpMethod':'POST',
             'authorization_scheme':'Bearer', 'authorization_credentials':key}}
    api('/api/v1/provisioning/contact-points'+('/'+cfg['receiver'] if exists else ''),
        point, 'PUT' if exists else 'POST')
    print('Grafana contact point provisioned (credential redacted)')
    if args.activate:
        cfg['enabled'] = True
        (HERE / 'notifications.json').write_text(json.dumps(cfg, indent=2)+'\n')
    if cfg['enabled']:
        rules = api('/api/v1/provisioning/alert-rules')
        owned = {r['uid'] for r in json.loads((HERE/'alerts/rules.json').read_text())}
        for rule in rules:
            if rule['uid'] not in owned:
                continue
            rule['notification_settings'] = ({'receiver':'unwired'} if rule['uid'] == 'sh-sms-gateway-down'
                else {k:cfg[k] for k in ['receiver','group_wait','group_interval','repeat_interval']})
            api('/api/v1/provisioning/alert-rules/'+rule['uid'],rule,'PUT')
        print('Grafana rules now send through ntfy-primary')
    else:
        print('STAGED: Cloudflare route and --activate are still required')


if __name__ == '__main__':
    main()
