#!/usr/bin/env python3
"""Maintain internal TLS credentials in Kubernetes Secrets; private temp files use tmpfs."""
import base64, json, os, pathlib, subprocess, tempfile

def run(args): return subprocess.check_output(args,stderr=subprocess.DEVNULL)
def secret(ns,name):
    p=subprocess.run(['kubectl','get','secret','-n',ns,name,'--ignore-not-found','-o','json'],capture_output=True)
    if p.returncode: raise RuntimeError('Cannot read TLS Secret '+ns+'/'+name)
    if not p.stdout.strip(): return {}
    return {k:base64.b64decode(v) for k,v in json.loads(p.stdout)['data'].items()}
def save(ns,name,data):
    if secret(ns,name)==data:return
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':ns},'type':'Opaque','data':{k:base64.b64encode(v).decode() for k,v in data.items()}}
    subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(obj).encode(),check=True,stdout=subprocess.DEVNULL)
    if name in ('buildkit-server-tls','registry-internal-tls'):
        deployment='buildkit' if name=='buildkit-server-tls' else 'registry'
        if subprocess.run(['kubectl','get','deployment','-n',ns,deployment],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
            subprocess.run(['kubectl','rollout','restart','deployment','-n',ns,deployment],check=True,stdout=subprocess.DEVNULL)
def valid(cert,days):
    p=pathlib.Path('check.pem');p.write_bytes(cert)
    return subprocess.run(['openssl','x509','-in',str(p),'-checkend',str(days*86400),'-noout'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0

def main():
    os.umask(0o077)
    for ns in ['buildkit']:
        if subprocess.run(['kubectl','get','namespace',ns],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            subprocess.run(['kubectl','create','namespace',ns],check=True,stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='internal-pki-') as tmp:
        os.chdir(tmp)
        ca=secret('buildkit','internal-build-ca')
        if not ca:
            run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','1825','-subj','/CN=Homelab build infrastructure CA','-keyout','ca.key','-out','ca.crt','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign'])
            ca={n:pathlib.Path(n).read_bytes() for n in ['ca.key','ca.crt']};save('buildkit','internal-build-ca',ca)
        assert valid(ca['ca.crt'],120),'CA needs planned rotation before renewing leaves'
        for n,v in ca.items():pathlib.Path(n).write_bytes(v)
        def issue(ns,name,cn,san,usage):
            old=secret(ns,name)
            if old and old.get('ca.crt')==ca['ca.crt'] and valid(old['tls.crt'],30) and os.environ.get('PKI_RENEW_CLIENT')!=ns:return old
            run(['openssl','req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+cn,'-keyout','leaf.key','-out','leaf.csr'])
            pathlib.Path('ext').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage='+usage+'\nsubjectAltName='+san+'\n')
            run(['openssl','x509','-req','-in','leaf.csr','-CA','ca.crt','-CAkey','ca.key','-set_serial','0x'+os.urandom(16).hex(),'-days','90','-extfile','ext','-out','leaf.crt'])
            data={'ca.crt':ca['ca.crt'],'tls.crt':pathlib.Path('leaf.crt').read_bytes(),'tls.key':pathlib.Path('leaf.key').read_bytes()};save(ns,name,data);return data
        issue('buildkit','buildkit-server-tls','buildkitd','DNS:buildkitd.buildkit.svc.cluster.local,DNS:buildkitd.buildkit.svc,DNS:localhost,IP:127.0.0.1','serverAuth')
        issue('infra','registry-internal-tls','registry','IP:10.43.200.10,DNS:registry.infra.svc.cluster.local,DNS:registry.zachd.duckdns.org','serverAuth')
        namespaces=json.loads(run(['kubectl','get','namespaces','-o','json']))['items']
        for ns in namespaces:
            name=ns['metadata']['name']
            if name=='claude' or 'actions-runner.zachd/repo' in ns['metadata'].get('labels',{}):
                issue(name,'buildkit-client-tls',name,'URI:urn:homelab:builder:'+name,'clientAuth')
    print('Internal TLS certificates ready (90-day leaves; renew with this command).')
if __name__=='__main__':main()
