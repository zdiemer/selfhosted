"""External TURN smoke test; requires an authenticated 1Password CLI session.

Creates one short-lived allocation, verifies private-peer rejection, then releases
it. Sends no relayed traffic. Credentials stay in memory and are never printed.
"""
import hashlib,hmac,json,os,socket,struct,subprocess
from pathlib import Path
import yaml
config=yaml.safe_load(Path(__file__).with_name('config.yml').read_text())
secret=json.loads(subprocess.check_output(['op','read','op://homelab/egress-sfo3-production/secrets.json']))
password=secret['/etc/coturn/turn_password']['content'].strip()
del secret
username=config['turn_user'].encode()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(6);s.connect((config['public_ipv4'],3478))
cookie=0x2112A442
realm=nonce=key=None

def attr(k,v):return struct.pack('!HH',k,len(v))+v+b'\0'*((-len(v))%4)
def request(kind,attributes,auth=False):
 tx=os.urandom(12); body=b''.join(attr(k,v) for k,v in attributes)
 if auth:
  body+=attr(6,username)+attr(0x14,realm)+attr(0x15,nonce)
 hdr=struct.pack('!HHI12s',kind,len(body)+(24 if auth else 0),cookie,tx)
 if auth:body+=attr(8,hmac.new(key,hdr+body,hashlib.sha1).digest())
 s.send(hdr+body)
 raw=s.recv(4096);typ,length,magic,transaction=struct.unpack('!HHI12s',raw[:20]);assert magic==cookie and transaction==tx
 parsed={};pos=20
 while pos<20+length:
  k,l=struct.unpack('!HH',raw[pos:pos+4]);parsed[k]=raw[pos+4:pos+4+l];pos+=4+((l+3)//4)*4
 return typ,parsed

def error(attrs):
 v=attrs.get(9,b'\0'*4);return v[2]*100+v[3]
typ,a=request(3,[(0x19,b'\x11\0\0\0')]);assert error(a)==401,'TURN did not require credentials'
realm=a[0x14];nonce=a[0x15];key=hashlib.md5(username+b':'+realm+b':'+password.encode()).digest()
typ,a=request(3,[(0x19,b'\x11\0\0\0'),(0x0d,struct.pack('!I',60))],True)
assert typ==0x103, f'TURN allocation failed: {error(a)}'
try:
 peer=struct.pack('!BBHI',0,1,9^(cookie>>16),int.from_bytes(socket.inet_aton('127.0.0.1'),'big')^cookie)
 typ,a=request(8,[(0x12,peer)],True);assert error(a)==403,'TURN did not reject loopback peer'
 print('TURN: anonymous access challenged; authenticated allocation works; loopback peer denied')
finally:
 typ,a=request(4,[(0x0d,struct.pack('!I',0))],True)
 assert typ==0x104, 'TURN test allocation cleanup failed'
 print('TURN: test allocation released')
