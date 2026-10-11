"""Mirror upstream tools/wasm/serve.py's asset manifest on a read-only NAS mount."""
import json
from pathlib import Path

source = Path('/nas')
assets = Path('/assets')
for target in ('game', 'gamemd'):
    destination = assets / 'files' / target
    destination.mkdir(parents=True, exist_ok=True)
    names = {'blowfish.dll', target + '.exe'}
    manifest = []
    for path in sorted(source.iterdir()):
        name = path.name.lower()
        if path.is_file() and (name in names or (
            path.suffix.lower() in {'.mix', '.mmx', '.yro', '.map', '.ini', '.csf', '.pkt', '.tlb'}
            and (target == 'gamemd' or 'md' not in name)
        )):
            (destination / path.name).symlink_to(path)
            manifest.append({'name': path.name, 'size': path.stat().st_size})
    present = {entry['name'].lower() for entry in manifest}
    required = {target + '.exe', 'blowfish.dll', 'ra2.mix', 'language.mix', 'movies01.mix', 'movies02.mix'}
    if target == 'gamemd':
        required |= {'ra2md.mix', 'langmd.mix', 'movmd03.mix'}
    if missing := required - present:
        raise SystemExit(f'Missing {target} NAS assets: {sorted(missing)}')
    (assets / (target + '.json')).write_text(json.dumps(manifest))
