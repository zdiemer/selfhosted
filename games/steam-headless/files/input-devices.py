"""Expose virtual input nodes only; never publish physical host input devices."""
import os
from pathlib import Path
import stat
import signal
import sys
import time


def discover(class_root=Path('/sys/class/input')):
    desired = {}
    for entry in class_root.glob('*'):
        if '/devices/virtual/input/' not in str(entry.resolve()):
            continue
        try:
            major, minor = map(int, (entry / 'dev').read_text().strip().split(':'))
        except (OSError, ValueError):
            continue
        if major != 13 or not entry.name.startswith(('event', 'js', 'mouse')):
            continue
        desired[entry.name] = os.makedev(major, minor)
    return desired


def main():
    if os.environ.get('STEAM_INPUT_POLICY') != 'virtual-input-v1':
        raise SystemExit('Required NRI input-device policy is absent')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    root = Path('/dev/input')
    root.mkdir(exist_ok=True)
    while True:
        desired = discover()
        for path in root.iterdir():
            if path.name not in desired or path.stat().st_rdev != desired[path.name]:
                path.unlink()
        for name, device in desired.items():
            path = root / name
            if not path.exists():
                os.mknod(path, stat.S_IFCHR | 0o660, device)
                os.chown(path, 1000, 1000)
        time.sleep(0.25)


if __name__ == '__main__':
    main()
