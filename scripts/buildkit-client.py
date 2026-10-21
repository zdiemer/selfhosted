#!/usr/bin/env python3
"""Export workspace BuildKit client TLS credentials for buildctl/buildx."""
import base64,json,os,pathlib,subprocess
os.umask(0o077)
data=json.loads(subprocess.check_output(['kubectl','get','secret','-n','claude','buildkit-client-tls','-o','json']))['data']
root=pathlib.Path.home()/'.config/buildkit/tls';root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
for name,value in data.items():
 p=root/name;p.write_bytes(base64.b64decode(value));p.chmod(0o600)
print(root)
