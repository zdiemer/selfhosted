"""Keep console stdin open; SIGTERM requests Terraria's save-and-exit path."""
import os
from pathlib import Path
import shutil
import signal
import subprocess

data = Path('/data')
(data / 'Mods').mkdir(exist_ok=True)
(data / 'Worlds').mkdir(exist_ok=True)
# The image's whole mod set is authoritative; stale mods must not survive upgrades.
for old in (data / 'Mods').glob('*.tmod'):
    old.unlink()
for source in Path('/opt/mods').iterdir():
    shutil.copyfile(source, data / 'Mods' / source.name)
config = Path('/config/serverconfig.txt').read_text()
config += '\npassword=' + os.environ.get('SERVER_PASSWORD', '') + '\n'
(data / 'serverconfig.txt').write_text(config)
process = subprocess.Popen(['dotnet', '/opt/tml/tModLoader.dll', '-server', '-nosteam',
    '-config', '/data/serverconfig.txt', '-tmlsavedirectory', '/data'], stdin=subprocess.PIPE, text=True)
def stop(*_):
    if process.poll() is None:
        try:
            process.stdin.write('exit\n')
            process.stdin.flush()
        except BrokenPipeError:
            pass
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
raise SystemExit(process.wait())
