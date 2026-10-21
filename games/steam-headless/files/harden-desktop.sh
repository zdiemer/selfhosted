#!/usr/bin/env bash
# Runs after upstream init, before supervisord opens any listeners.
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
p = Path('/usr/bin/start-x11vnc.sh')
s = p.read_text()
needle = '/usr/bin/x11vnc -display'
assert s.count(needle) == 1, 'upstream VNC startup changed'
p.write_text(s.replace(needle, '/usr/bin/x11vnc -localhost -display'))
p = Path('/opt/frontend/utils/run.sh')
s = p.read_text()
assert s.count('${WEBSOCKIFY} --web ') == 1
assert s.count('${WEBSOCKIFY} ${PORT_AUDIO_WEBSOCKET:?}') == 1
s = s.replace('${WEBSOCKIFY} --web ', '${WEBSOCKIFY} --auth-plugin SteamAutheliaAuth --web-auth --web ')
s = s.replace('${WEBSOCKIFY} ${PORT_AUDIO_WEBSOCKET:?}', '${WEBSOCKIFY} --auth-plugin SteamAutheliaAuth ${PORT_AUDIO_WEBSOCKET:?}')
p.write_text(s)
p = Path('/opt/frontend/utils/websockify/websockify/auth_plugins.py')
p.write_text(p.read_text() + Path('/opt/chart-init/steam-auth.py').read_text())
p = Path('/etc/sudoers')
s = p.read_text()
s = '\n'.join(line for line in s.splitlines() if not (line.strip().startswith('default ') and 'NOPASSWD' in line)) + '\n'
p.write_text(s)
PY
visudo -c
