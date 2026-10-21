#!/usr/bin/env python3
import json, pathlib, sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'scripts/lib'))
from node_exec import node_session
root=pathlib.Path(__file__).resolve().parent
code='''import json,pathlib,subprocess,sys
for name,content in json.load(sys.stdin).items():
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content);p.chmod(0o644)
subprocess.run(['/usr/sbin/apparmor_parser','-r','/etc/apparmor.d/buildkit'],check=True)
pathlib.Path('/etc/sysctl.d/60-buildkit-userns.conf').write_text('kernel.apparmor_restrict_unprivileged_userns=1\\n')
subprocess.run(['sysctl','-w','kernel.apparmor_restrict_unprivileged_userns=1'],check=True)
'''
with node_session('zachd-ubuntu-4') as execute:
 execute(['python3','-c',code],json.dumps({'/etc/apparmor.d/buildkit':(root/'profiles/buildkit.apparmor').read_text(),'/var/lib/kubelet/seccomp/buildkit.json':(root/'profiles/buildkit.json').read_text()}).encode())
print('BuildKit node profiles installed')
