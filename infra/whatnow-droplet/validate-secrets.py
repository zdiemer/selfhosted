"""Fail closed without printing values. Preserve dotenv bytes during migration."""
import json
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
env = (p / 'app.env').read_text()
for key in ('STEAM_API_KEY', 'SESSION_SECRET', 'PASSWORD_PEPPER'):
    match = re.search(r'^' + key + r'=([^\n]*)', env, re.M)
    if not match or match[1].strip() in ('', '""', "''"):
        sys.exit(f'Missing required production field: {key}')
if re.search(r'^WHATNOW_TAG=', env, re.M):
    sys.exit('Release pin belongs in config.yml, not the vault environment')
auth = json.loads((p / 'docker.json').read_text())
if not auth.get('auths', {}).get('ghcr.io', {}).get('auth'):
    sys.exit('Missing GHCR pull authentication')
