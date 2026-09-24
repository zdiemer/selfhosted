"""Queue metadata backfill inside RomM from /backend with its Python.

Default: Unmatched IGDB + Hasheous scans, after existing queued work.
--hashes: selected-ROM Unmatched scans with hashing enabled, in batches of 100.
These reuse unchanged hashes and preserve existing provider matches. Run the
normal metadata pass first so covers aren't delayed by large disc hash reads.
Pass filesystem folder names to narrow either pass. --dry-run prints the plan.
"""
import argparse
import hashlib
from pathlib import Path

from sqlalchemy import select
from rq.job import Job
from config import SCAN_TIMEOUT, TASK_RESULT_TTL
from config.config_manager import config_manager
from endpoints.sockets.scan import scan_platforms, report_scan_failure, scan_job_meta
from handler.database.base_handler import sync_session
from handler.filesystem.roms_handler import NON_HASHABLE_PLATFORMS
from handler.metadata import meta_igdb_handler, meta_hasheous_handler
from handler.redis_handler import redis_client, scan_queue
from models.platform import Platform
from models.rom import Rom
from handler.scan_handler import ScanType

FOLDERS = [
    'GameCube', 'Wii', 'WiiWare', 'PS2', 'Dreamcast', 'Nintendo 3DS',
    'New Nintendo 3DS', 'Atari ST', 'Intellivision', 'MSX', 'MSX Turbo R',
    'MSX2', 'Neo Geo CD', 'Odyssey 2', 'PS3', 'PS4', 'SG-1000', 'ScummVM',
    'Switch', 'Vectrex', 'Wii U', 'Xbox', 'Xbox 360', 'ZX Spectrum',
]
# RomM hashes these containers as files, not as their original disc contents.
OPAQUE_DISC_FORMATS = {'.rvz', '.gcz', '.ciso', '.wbfs', '.cso', '.zso'}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hashes', action='store_true')
parser.add_argument('--dry-run', action='store_true')
parser.add_argument('folders', nargs='*')
args = parser.parse_args()
folders = args.folders or FOLDERS
if not (meta_igdb_handler.is_enabled() and meta_hasheous_handler.is_enabled()):
    raise SystemExit('IGDB and Hasheous must both be enabled')
if args.hashes and config_manager.get_config().SKIP_HASH_CALCULATION:
    raise SystemExit('Disable skip_hash_calculation before hash backfill')
for folder in folders:
    if not (Path('/romm/library/Roms') / folder).is_dir():
        raise SystemExit(f'Platform folder does not exist: {folder}')

plans = []
for folder in folders:
    if not args.hashes:
        plans.append((folder, []))
        continue
    with sync_session() as session:
        platform = session.scalar(select(Platform).where(Platform.fs_slug == folder))
        if not platform or not platform.hasheous_id or platform.slug in NON_HASHABLE_PLATFORMS:
            print(f'Skip hash backfill: {folder} (not indexed or not supported)')
            continue
        rows = session.execute(select(Rom.id, Rom.fs_name).where(
            Rom.platform_id == platform.id,
            Rom.hasheous_id.is_(None),
            Rom.missing_from_fs.is_(False),
        ).order_by(Rom.id)).all()
        ids = [rom_id for rom_id, name in rows
               if Path(name).suffix.lower() not in OPAQUE_DISC_FORMATS]
        for offset in range(0, len(ids), 100):
            plans.append((folder, ids[offset:offset + 100]))

for folder, ids in plans:
    phase = 'hash-match' if args.hashes else 'metadata'
    digest = hashlib.sha256(f'{phase}:{folder}:{ids}'.encode()).hexdigest()[:24]
    job_id = f'streaming-{phase}-{digest}'
    if Job.exists(job_id, connection=redis_client):
        print(f'Already recorded: {job_id}; inspect its status before retrying')
        continue
    print(f'{phase}: {folder}: {len(ids) if ids else "platform"}: {job_id}')
    if args.dry_run:
        continue
    scan_queue.enqueue(
        scan_platforms, job_id=job_id,
        platform_ids=[], metadata_sources=['igdb', 'hasheous'],
        scan_type=ScanType.UNMATCHED, roms_ids=ids,
        platform_fs_slugs=[folder], launchbox_remote_enabled=False,
        on_failure=report_scan_failure,
        job_timeout=SCAN_TIMEOUT, result_ttl=TASK_RESULT_TTL,
        meta={**scan_job_meta(ScanType.UNMATCHED),
              'task_name': f'{phase}: {folder}', 'streaming_match': phase},
    )
