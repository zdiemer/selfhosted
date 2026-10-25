#!/usr/bin/env python3
"""Bound all regenerable artwork, leaving database files outside the cache alone."""
import fcntl
import heapq
import os
from pathlib import Path
import stat

GIB = 1024 ** 3


def available_bytes(path):
    fs = os.statvfs(path)
    return fs.f_bavail * fs.f_frsize


def evict(root, ceiling=8 * GIB, reserve=3 * GIB, batch=100000):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return 0
    removed = 0
    while True:
        used = 0

        def entries():
            nonlocal used
            for directory, _, names in os.walk(root, followlinks=False):
                for name in names:
                    path = os.path.join(directory, name)
                    try:
                        info = os.stat(path, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        used += info.st_blocks * 512
                        yield info.st_mtime_ns, path, info.st_blocks * 512

        # Bounded memory, no temporary sort files on an already-full disk.
        oldest = heapq.nsmallest(batch, entries())
        needed = max(used - ceiling, reserve - available_bytes(root), 0)
        if needed <= 0:
            return removed
        if not oldest:
            raise RuntimeError('Artwork exhausted; disk reserve still unavailable')
        freed = 0
        for _, path, size in oldest:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            freed += size
            removed += 1
            if freed >= needed:
                break
        print(f'Artwork cache: removed {freed} bytes; {removed} files total', flush=True)
        # Rescan after each batch: concurrent fetches and disk users can change
        # both totals. Empty negative-cache markers are also safe to evict.


if __name__ == '__main__':
    with open('/run/lock/whatnow-art-evict.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        evict('/opt/whatnowgg/data/art')
