"""One-time secret adoption. Requires authenticated SSH and 1Password sessions.

No values are printed, written locally, regenerated, or put on command lines.
Review the live non-secret configuration separately before enabling the playbook.
"""
import json
import subprocess


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout


items = json.loads(run(['op', 'item', 'list', '--vault', 'homelab', '--format=json']))
if any(item['title'] == 'egress-sfo3-production' for item in items):
    raise SystemExit('Vault item already exists; refusing to overwrite')
remote = '''import json, pathlib, stat, pwd, grp
paths = [
 '/etc/squid/secret/passwd', '/etc/squid/secret/cluster_password',
 '/etc/coturn/turn_password',
 '/etc/squid/tls/proxy.key', '/etc/squid/tls/proxy.crt', '/etc/squid/tls/proxy.pem',
 '/etc/crowdsec/local_api_credentials.yaml', '/etc/crowdsec/online_api_credentials.yaml',
 '/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml',
 '/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml.local']
result = {}
for name in paths:
 p = pathlib.Path(name)
 if p.is_file():
  st = p.stat()
  result[name] = dict(content=p.read_text(), mode=format(stat.S_IMODE(st.st_mode), '04o'),
                     owner=pwd.getpwuid(st.st_uid).pw_name, group=grp.getgrgid(st.st_gid).gr_name)
for name in paths[:3]:
 assert name in result and result[name]['content'], 'Required service credential missing'
assert any('/bouncers/' in name for name in result), 'Bouncer configuration missing'
print(json.dumps(result))
'''
secrets = run(['ssh', '-o', 'BatchMode=yes', 'egress-sfo3', 'python3 -'], input=remote.encode())
json.loads(secrets)
item = {'title': 'egress-sfo3-production', 'category': 'SECURE_NOTE',
        'fields': [{'id': 'secretsjson', 'label': 'secrets.json',
                    'type': 'CONCEALED', 'value': secrets.decode()}]}
run(['op', 'item', 'create', '--vault', 'homelab', '--format=json'], input=json.dumps(item).encode())
retrieved = run(['op', 'read', 'op://homelab/egress-sfo3-production/secrets.json'])
assert json.loads(retrieved) == json.loads(secrets)
print('Egress credentials migrated and vault round trip verified; no values displayed.')
