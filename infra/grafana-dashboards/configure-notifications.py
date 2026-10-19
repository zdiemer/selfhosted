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
from notification_policy import notification_settings

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
            'Authorization': 'Bearer '+key, 'Content-Type':'application/json', 'User-Agent':'Grafana'})
        try:
            urllib.request.build_opener(NoRedirect).open(request, timeout=15)
            raise RuntimeError('Webhook accepted invalid payload; refusing activation')
        except urllib.error.HTTPError as error:
            if error.code != 400 or json.loads(error.read()).get('detail') != 'invalid Grafana webhook':
                raise RuntimeError('Public webhook is not ready; configure Cloudflare first') from None
    # Derive the recipient from the existing sole Grafana administrator. Refuse
    # ambiguity rather than silently mailing newly added organization members.
    admins = [u['email'] for u in api('/api/org/users')
              if u.get('role') == cfg['email_user_role'] and u.get('email')]
    if len(admins) != 1:
        raise RuntimeError('Expected exactly one Grafana administrator email')
    points = api('/api/v1/provisioning/contact-points')
    existing = {p['uid'] for p in points}
    webhook = {'url':cfg['url'], 'httpMethod':'POST',
               'authorization_scheme':'Bearer', 'authorization_credentials':key}
    desired = [
        {'uid':cfg['receiver'], 'name':cfg['receiver'], 'type':'webhook', 'settings':webhook},
        {'uid':'critical-outages-webhook', 'name':cfg['critical_receiver'],
         'type':'webhook', 'settings':webhook},
        {'uid':'critical-outages-email', 'name':cfg['critical_receiver'],
         'type':'email', 'settings':{'addresses':admins[0], 'singleEmail':True}},
    ]
    desired.append({'uid':'notification-path-email', 'name':'notification-path-email',
                    'type':'email', 'settings':{'addresses':admins[0], 'singleEmail':True}})
    for point in desired:
        point['disableResolveMessage'] = False
        exists = point['uid'] in existing
        api('/api/v1/provisioning/contact-points'+('/'+point['uid'] if exists else ''),
            point, 'PUT' if exists else 'POST')
    print('Grafana webhook and independent critical email provisioned (credentials redacted)')
    if args.activate:
        cfg['enabled'] = True
    if cfg['enabled']:
        rules = api('/api/v1/provisioning/alert-rules')
        owned = {r['uid'] for r in json.loads((HERE/'alerts/rules.json').read_text())}
        for rule in rules:
            if rule['uid'] not in owned:
                continue
            rule['notification_settings'] = notification_settings(rule, cfg)
            api('/api/v1/provisioning/alert-rules/'+rule['uid'],rule,'PUT')
        (HERE / 'notifications.json').write_text(json.dumps(cfg, indent=2)+'\n')
        print('Grafana routing active: warnings via relay; critical via relay and cloud email')
    else:
        print('STAGED: Cloudflare route and --activate are still required')


if __name__ == '__main__':
    main()
