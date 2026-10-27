"""Bake Workshop mods into the image; never update them during pod startup."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

mods = json.loads(Path('/opt/mods.json').read_text())
args = ['/opt/steam/steamcmd.sh', '+login', 'anonymous']
for mod in mods:
    args += ['+workshop_download_item', '1281930', mod['workshop']]
subprocess.run(args + ['+quit'], check=True)
lock = []
for mod in mods:
    root = Path('/home/terraria/Steam/steamapps/workshop/content/1281930') / mod['workshop']
    candidates = []
    for path in root.rglob('*.tmod'):
        try:
            version = tuple(map(int, path.parent.name.split('.')))
        except ValueError:
            continue
        if version <= (2026, 8):
            candidates.append((version, path))
    if not candidates:
        raise RuntimeError(f"No compatible tModLoader 2026.08 build: {mod}")
    _, source = max(candidates)
    target = Path('/opt/mods') / (mod['name'] + '.tmod')
    shutil.copyfile(source, target)
    lock.append(dict(mod, source=str(source.relative_to(root)), sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
Path('/opt/mods/lock.json').write_text(json.dumps(lock, indent=2))
Path('/opt/mods/enabled.json').write_text(json.dumps([m['name'] for m in mods] + ['ClaudeCompanion']))
print(json.dumps(lock, indent=2))
