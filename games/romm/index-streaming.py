"""Queue missing platform indexes inside RomM, from /backend with its Python.

Existing platforms are retained. Pass filesystem folder names as arguments to
resume a partially indexed platform. New entries calculate hashes and query
IGDB plus Hasheous. Existing entries need match-streaming.py to backfill
missing metadata and hashes.
"""
import sys
from config import SCAN_TIMEOUT, TASK_RESULT_TTL
from config.config_manager import config_manager
from endpoints.sockets.scan import scan_platforms, report_scan_failure, scan_job_meta
from handler.database import db_platform_handler
from handler.redis_handler import scan_queue
from handler.scan_handler import ScanType
from handler.scan_jobs import get_blocking_library_scans
from pathlib import Path

config = config_manager.get_config()
if config.SKIP_HASH_CALCULATION:
    raise SystemExit('Disable skip_hash_calculation for Hasheous matching')
running, queued = get_blocking_library_scans()
if running or queued:
    raise SystemExit('A library scan is already running or queued; do not duplicate it')
existing = {p.fs_slug for p in db_platform_handler.get_platforms()}
folders = sys.argv[1:] or [p.name for p in Path('/romm/library/Roms').iterdir()
                          if p.is_dir() and p.name not in existing]
priority = ['GameCube', 'Wii', 'WiiWare', 'PS2', 'Dreamcast', 'Nintendo 3DS', 'New Nintendo 3DS']
folders.sort(key=lambda x: (priority.index(x) if x in priority else len(priority), x))
for folder in folders:
    job = scan_queue.enqueue(
        scan_platforms,
        platform_ids=[], metadata_sources=["igdb", "hasheous"], scan_type=ScanType.QUICK,
        roms_ids=[], platform_fs_slugs=[folder], launchbox_remote_enabled=False,
        on_failure=report_scan_failure,
        job_timeout=SCAN_TIMEOUT, result_ttl=TASK_RESULT_TTL,
        meta={**scan_job_meta(ScanType.QUICK), 'task_name': f'Index {folder}',
              'index_platform': folder},
    )
    print(f'{folder}: {job.id}')
print(f'Queued {len(folders)} platform indexes; existing catalog retained.')
