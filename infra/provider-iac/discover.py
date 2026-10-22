#!/usr/bin/env python3
"""Read provider inventory into private review files. Never mutates a provider."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
from run import vault_read

EXPECTED = {'whatnow': '64.227.0.147', 'egress': '147.182.206.23'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Provider redirect refused; authorization cannot follow another host')


class API:
    def __init__(self, base, token, basic=False):
        self.base = base
        self.auth = ('Basic ' + base64.b64encode((token + ':').encode()).decode()
                     if basic else 'Bearer ' + token)
        self.opener = urllib.request.build_opener(NoRedirect)

    def get(self, path, accept='application/json'):
        request = urllib.request.Request(self.base + path, headers={
            'Authorization': self.auth, 'Accept': accept, 'User-Agent': 'selfhosted-provider-iac/1'})
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read().decode()
        except urllib.error.HTTPError as error:
            raise ValueError('Provider read failed with HTTP ' + str(error.code)) from None
        return json.loads(data) if accept == 'application/json' else data

    def pages(self, path, key, cloudflare=False):
        results = []
        for page in range(1, 101):
            separator = '&' if '?' in path else '?'
            response = self.get(path + separator + urllib.parse.urlencode({'page': page, 'per_page': 100}))
            if cloudflare and response.get('success') is not True:
                raise ValueError('Cloudflare did not report a successful read')
            batch = response[key]
            results.extend(batch)
            if cloudflare:
                if page >= response['result_info']['total_pages']:
                    return results
            elif not response.get('links', {}).get('pages', {}).get('next'):
                return results
        raise ValueError('Pagination limit reached; refusing incomplete inventory')


def select_droplets(droplets):
    result = {}
    for key, ip in EXPECTED.items():
        matches = [d for d in droplets if ip in [n['ip_address'] for n in d['networks']['v4'] if n['type'] == 'public']]
        if len(matches) != 1:
            raise ValueError('Expected exactly one droplet for ' + key)
        result[key] = matches[0]
    if str(result['whatnow']['id']) != '587384488':
        raise ValueError('Whatnow provider ID changed; review the production inventory')
    return result


def attached_firewalls(droplet, firewalls):
    return [f for f in firewalls if droplet['id'] in f.get('droplet_ids', []) or
            set(droplet['tags']) & set(f.get('tags', []))]


def do_inventory(api, ssh):
    selected = select_droplets(api.pages('/v2/droplets', 'droplets'))
    firewalls = api.pages('/v2/firewalls', 'firewalls')
    inventory = {'droplets': {}, 'firewall_ids': {}, 'ssh_source_cidrs': ssh}
    for key, d in selected.items():
        if 'backups' not in d['features']:
            raise ValueError('Backups are disabled for ' + key + '; review enabling cost/schedule explicitly')
        response = api.get(f'/v2/droplets/{d["id"]}/backups/policy')['policy']
        if response.get('droplet_id') != d['id'] or response.get('backup_enabled') is not True:
            raise ValueError('Backup policy identity/enabled state mismatch for ' + key)
        policy = response['backup_policy']
        if policy.get('plan') not in ['daily', 'weekly'] or policy.get('hour') is None:
            raise ValueError('Unsupported or incomplete backup policy for ' + key)
        inventory['droplets'][key] = dict(id=str(d['id']), name=d['name'], image=str(d['image']['id']),
            region=d['region']['slug'], size=d['size_slug'], ipv4=EXPECTED[key], ipv6='ipv6' in d['features'],
            vpc_uuid=d['vpc_uuid'], tags=d['tags'], volume_ids=d['volume_ids'],
            backup_policy={k: policy[k] for k in ['plan', 'weekday', 'hour'] if policy.get(k) is not None})
        attached = attached_firewalls(d, firewalls)
        if len(attached) > 1 or any(f.get('tags') or set(f.get('droplet_ids', [])) != {d['id']} for f in attached):
            raise ValueError('Shared/tag-based or multiple firewalls apply to ' + key + '; reconcile ownership before adoption')
        if attached:
            if attached[0].get('pending_changes') or attached[0].get('status') != 'succeeded':
                raise ValueError('Firewall not settled for ' + key)
            inventory['firewall_ids'][key] = attached[0]['id']
    return inventory


def dns_inventory(api, zone_name, names):
    zones = api.pages('/zones?' + urllib.parse.urlencode({'name': zone_name}), 'result', True)
    if len(zones) != 1:
        raise ValueError('Expected exactly one visible DNS zone')
    zone = zones[0]['id']
    records = api.pages(f'/zones/{zone}/dns_records', 'result', True)
    selected = [r for r in records if r['name'] in names and r['type'] in ['A', 'AAAA', 'CNAME']]
    if {r['name'] for r in selected} != set(names):
        raise ValueError('One or more selected names have no A/AAAA/CNAME record')
    return {'records': {r['name'] + '-' + r['type'] + '-' + r['id']: dict(
        id=r['id'], zone_id=zone, name=r['name'], type=r['type'], content=r['content'],
        ttl=r['ttl'], proxied=r['proxied'], comment=r.get('comment'), tags=r.get('tags', [])) for r in selected}}


def write_private(path, content):
    with path.open('x') as output:
        output.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stack', choices=['digitalocean', 'cloudflare', 'tailscale'])
    parser.add_argument('--output', type=Path, required=True, help='New private directory; never overwritten')
    parser.add_argument('--credential-ref')
    parser.add_argument('--ssh-cidr', action='append', default=[])
    parser.add_argument('--zone')
    parser.add_argument('--name', action='append', default=[])
    parser.add_argument('--tailnet', default='-')
    args = parser.parse_args()
    os.umask(0o077)
    token = vault_read(args.credential_ref or f'op://homelab/provider-iac-{args.stack}/token')
    if args.stack == 'digitalocean':
        if not args.ssh_cidr:
            raise ValueError('Supply verified operator public --ssh-cidr values before generating firewall inputs')
        for cidr in args.ssh_cidr:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.prefixlen == 0 or not network.network_address.is_global:
                raise ValueError('SSH source must be a specific public CIDR')
        data = do_inventory(API('https://api.digitalocean.com', token), args.ssh_cidr)
    elif args.stack == 'cloudflare':
        if not args.zone or not args.name:
            raise ValueError('Specify --zone and selected --name values; never implicitly adopt a zone')
        data = dns_inventory(API('https://api.cloudflare.com/client/v4', token), args.zone, args.name)
    else:
        policy = API('https://api.tailscale.com', token, basic=True).get(
            '/api/v2/tailnet/' + urllib.parse.quote(args.tailnet, safe='') + '/acl', 'application/hujson')
        if not policy.strip():
            raise ValueError('Empty tailnet policy response')
        data = {'policy_path': str((args.output / 'policy.hujson').resolve())}
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    if args.stack == 'tailscale':
        write_private(args.output / 'policy.hujson', policy)
    write_private(args.output / 'inventory.tfvars.json', json.dumps(data, indent=2) + '\n')
    print('Read-only inventory saved for review: ' + str(args.output))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print('provider-iac: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
