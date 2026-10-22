#!/usr/bin/env python3
"""From a real peer node, prove exporter denial and kubelet reachability."""
import argparse
import json
import subprocess

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--source-node', default='zachd-ubuntu-5')
args = ap.parse_args()
nodes = json.loads(subprocess.check_output(['kubectl', 'get', 'nodes', '-o', 'json']))['items']
targets = [(n['metadata']['name'], a['address']) for n in nodes
           for a in n['status']['addresses']
           if a['type'] == 'InternalIP' and ':' not in a['address']
           and n['metadata']['name'] != args.source_node]
controls = [a['address'] for n in nodes if 'node-role.kubernetes.io/control-plane' in n['metadata']['labels'] for a in n['status']['addresses'] if a['type'] == 'InternalIP' and ':' not in a['address']]
source_control = any(n['metadata']['name'] == args.source_node and 'node-role.kubernetes.io/control-plane' in n['metadata']['labels'] for n in nodes)
script = '''import concurrent.futures, socket
TARGETS = %s
CONTROLS = %s
SOURCE_CONTROL = %s
def connects(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=3): return True
    except OSError: return False
def check(target):
    name, ip = target
    # The allowed port is a routing control: a blackholed address is not proof
    # that the denied port was blocked by this firewall.
    allowed = connects(ip, 10250)
    denied = not connects(ip, 9100)
    assert allowed, (name, ip, 'kubelet unreachable; cannot verify route')
    assert denied, (name, ip, 'exporter exposed')
    print(name, ip, 'kubelet reachable, exporter blocked', flush=True)
    return 1
assert connects('127.0.0.1', 9100), 'local exporter unavailable'
for ip in CONTROLS:
    assert connects(ip,6443), ('API unavailable',ip)
    assert connects(ip,2379)==SOURCE_CONTROL, ('etcd boundary incorrect',ip)
print('API and etcd peer boundary passed', flush=True)
for port in (3777,8899,8900):
    assert not connects('192.168.4.26',port), ('development listener exposed',port)
print('Primary-node development listeners blocked', flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    print('Verified addresses:', sum(ex.map(check, TARGETS)))
''' % (repr(targets), repr(controls), repr(source_control))
subprocess.run(['tailscale', 'ssh', 'root@'+args.source_node, 'python3 -'],
               input=script, text=True, check=True, timeout=45)
