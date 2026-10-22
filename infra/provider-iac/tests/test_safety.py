import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from run import check_plan, encryption
from discover import do_inventory, dns_inventory, select_droplets, attached_firewalls, EXPECTED


def change(kind, actions, before=None, after=None, address=None):
    return {'complete': True, 'resource_changes': [{
        'address': address or kind + '.managed["whatnow"]', 'mode': 'managed', 'type': kind,
        'change': {'actions': actions, 'before': before, 'after': after}}]}


class PlanSafety(unittest.TestCase):
    def test_replacement_and_deletion_refused(self):
        for actions in [['delete'], ['delete', 'create'], ['create', 'delete'], ['forget']]:
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                check_plan(change('digitalocean_droplet', actions), 'digitalocean')

    def test_only_explicit_firewalls_can_be_created(self):
        check_plan(change('digitalocean_firewall', ['create']), 'digitalocean')
        for kind, stack in [('digitalocean_droplet', 'digitalocean'), ('cloudflare_dns_record', 'cloudflare'),
                            ('tailscale_acl', 'tailscale')]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                check_plan(change(kind, ['create']), stack)
        with self.assertRaises(ValueError):
            check_plan(change('digitalocean_firewall', ['create'], address='digitalocean_firewall.unexpected'), 'digitalocean')

    def test_droplet_resize_volume_and_backup_disabling_refused(self):
        before = {'backups': True, 'backup_policy': [{'plan': 'weekly'}], 'resize_disk': False,
                  'size': 's-1vcpu-1gb', 'volume_ids': ['retained']}
        for field, value in [('size', 's-2vcpu-4gb'), ('volume_ids', []), ('backups', False), ('resize_disk', True)]:
            after = dict(before, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_plan(change('digitalocean_droplet', ['update'], before, after), 'digitalocean')
        check_plan(change('digitalocean_droplet', ['update'], before,
                          dict(before, backup_policy=[{'plan': 'daily'}])), 'digitalocean')

    def test_adoption_has_no_mutations(self):
        check_plan(change('tailscale_acl', ['no-op']), 'tailscale', adoption=True)
        with self.assertRaises(ValueError):
            check_plan(change('tailscale_acl', ['update']), 'tailscale', adoption=True)
        with self.assertRaises(ValueError):
            check_plan({'complete': False}, 'tailscale')

    def test_passphrase_cannot_interpolate_hcl(self):
        text = encryption('x${file("secret")}-%{if true}')
        self.assertIn('$${file', text)
        self.assertIn('%%{if true}', text)
        self.assertEqual(text.count('enforced = true'), 2)


def droplets():
    return [dict(id=587384488 if key == 'whatnow' else 1234, name=key, image={'id': 1},
                 region={'slug': 'sfo3'}, size_slug='s-1vcpu-1gb', vpc_uuid='vpc', tags=[],
                 volume_ids=[], features=['backups'], networks={'v4': [{'ip_address': ip, 'type': 'public'}]})
            for key, ip in EXPECTED.items()]


class FakeDO:
    def __init__(self, hosts=None, firewalls=None):
        self.hosts = hosts if hosts is not None else droplets()
        self.firewalls = firewalls or []
    def pages(self, path, key):
        return self.hosts if key == 'droplets' else self.firewalls
    def get(self, path):
        return {'policy': {'droplet_id': int(path.split('/')[3]), 'backup_enabled': True,
                           'backup_policy': {'plan': 'weekly', 'weekday': 'TUE', 'hour': 0}}}


class DiscoverySafety(unittest.TestCase):
    def test_missing_ambiguous_and_changed_identity_refused(self):
        for ds in [[], droplets() + [droplets()[0]], [dict(droplets()[0], id=1), droplets()[1]]]:
            with self.assertRaises(ValueError): select_droplets(ds)

    def test_tag_and_id_firewall_associations(self):
        host = dict(droplets()[0], tags=['shared'])
        fs = [{'id': 'a', 'tags': ['shared']}, {'id': 'b', 'droplet_ids': [host['id']]}, {'id': 'c'}]
        self.assertEqual([f['id'] for f in attached_firewalls(host, fs)], ['a', 'b'])

    def test_discovery_preserves_midnight_backup_policy(self):
        result = do_inventory(FakeDO(), ['8.8.8.8/32'])
        self.assertEqual(result['droplets']['whatnow']['backup_policy']['hour'], 0)
        self.assertEqual(result['firewall_ids'], {})

    def test_shared_firewall_and_missing_backups_refused(self):
        shared = {'id': 'shared', 'droplet_ids': [587384488, 1234], 'tags': [], 'status': 'succeeded'}
        with self.assertRaises(ValueError): do_inventory(FakeDO(firewalls=[shared]), ['8.8.8.8/32'])
        ds = droplets(); ds[0]['features'] = []
        with self.assertRaises(ValueError): do_inventory(FakeDO(hosts=ds), ['8.8.8.8/32'])

    def test_dns_selection_ignores_unrelated_and_txt_records(self):
        class API:
            def pages(self, path, key, cf):
                if path.startswith('/zones?'): return [{'id': 'zone'}]
                return [dict(id='record', name='whatnow.gg', type='A', content='64.227.0.147', ttl=1, proxied=True),
                        dict(id='private', name='whatnow.gg', type='TXT', content='never-export'),
                        dict(id='other', name='other.gg', type='A', content='8.8.4.4')]
        data = dns_inventory(API(), 'whatnow.gg', ['whatnow.gg'])
        self.assertEqual(len(data['records']), 1)
        self.assertNotIn('never-export', json.dumps(data))
        with self.assertRaises(ValueError): dns_inventory(API(), 'whatnow.gg', ['missing.gg'])


if __name__ == '__main__': unittest.main()
