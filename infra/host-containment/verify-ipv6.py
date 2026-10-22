#!/usr/bin/env python3
"""Verify IPv6 drops from a disposable network namespace on one real node."""
import argparse
import subprocess
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--node', default='zachd-ubuntu-6')
args = ap.parse_args()
script = r'''
import json,socket,subprocess,uuid
suffix=uuid.uuid4().hex[:6]
ns='hc-'+suffix
host='hch'+suffix
peer='hcp'+suffix
prefix='fd00:face:'+suffix[:4]+':'+suffix[4:]+':'
server_ip=prefix+':1'
client_ip=prefix+':2'
def run(*a): return subprocess.check_output(a,text=True)
def counter(port):
    data=json.loads(run('nft','-j','list','table','inet','selfhosted_host_guard'))
    for item in data['nftables']:
        expr=item.get('rule',{}).get('expr',[])
        if any('drop' in e for e in expr) and any(e.get('match',{}).get('right') == port or
            (isinstance(e.get('match',{}).get('right'), dict) and
             port in e['match']['right'].get('set', [])) for e in expr):
            return next(e['counter']['packets'] for e in expr if 'counter' in e)
    raise AssertionError(('drop rule missing', port))
server=None
try:
    # Establish that the protected exporter actually listens on IPv6.
    with socket.create_connection(('::1',9100),timeout=3): pass
    run('ip','netns','add',ns)
    run('ip','link','add',host,'type','veth','peer','name',peer)
    run('ip','link','set',peer,'netns',ns)
    run('ip','-6','addr','add',server_ip+'/126','dev',host,'nodad')
    run('ip','link','set',host,'up')
    run('ip','netns','exec',ns,'ip','link','set','lo','up')
    run('ip','netns','exec',ns,'ip','-6','addr','add',client_ip+'/126','dev',peer,'nodad')
    run('ip','netns','exec',ns,'ip','link','set',peer,'up')
    server=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
    server.bind((server_ip,19105));server.listen(4)
    protected=(22,6443,2379,2380,3777,8899,8900,9100,10250,111,29644,8472)
    before={p:counter(p) for p in protected}
    child="""import socket
ip=%r
with socket.create_connection((ip,19105),timeout=3): pass
for port in (22,6443,2379,2380,3777,8899,8900,9100,10250,111,29644):
    try:
        s=socket.create_connection((ip,port),timeout=2)
    except OSError: print('IPv6 TCP',port,'blocked')
    else: s.close(); raise AssertionError(('unexpected access',port))
s=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM)
s.sendto(b'host-guard-verification',(ip,8472));s.close()
""" % server_ip
    print(run('ip','netns','exec',ns,'python3','-c',child),end='')
    for p in protected:
        assert counter(p)>before[p], ('IPv6 did not hit drop rule', p)
    print('IPv6 route control passed; IPv6 exporter listener confirmed; UDP 8472 hit drop rule')
finally:
    if server:server.close()
    subprocess.run(['ip','link','delete',host],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['ip','netns','delete',ns],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
'''
subprocess.run(['tailscale', 'ssh', 'root@'+args.node, 'python3 -'],
               input=script, text=True, check=True, timeout=50)
