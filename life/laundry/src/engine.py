"""The live machine registry: in-memory state, persisted after every reading.

State is held in memory AND written to SQLite on every sample. The memory copy
is what the state machine folds into; the SQLite copy exists so that a pod
restart — a helm upgrade, an eviction, a node reboot — resumes an open cycle
rather than forgetting it. Losing that copy means a load that finishes during
the restart window is never announced, and the failure is invisible: the pod
comes up healthy and simply says the machine is idle.
"""

from __future__ import annotations

import asyncio
import logging
import time

import config
import db
import notify
from detect import (
    RUNNING,
    CameOnline,
    CycleFinished,
    CycleStarted,
    FalseStart,
    MachineConfig,
    MachineState,
    WentOffline,
    observe,
    tick,
)

logger = logging.getLogger("laundry.engine")

MACHINES: dict[str, MachineConfig] = {}
STATES: dict[str, MachineState] = {}


def init() -> None:
    global MACHINES
    MACHINES = config.load_machines()
    db.init()
    for device in MACHINES:
        STATES[device] = db.load_state(device)
        st = STATES[device]
        if st.state == RUNNING:
            logger.info(
                "resuming open cycle on %s, started %.0fs ago",
                device, time.time() - (st.cycle_started_at or time.time()),
            )


def _handle_events(cfg: MachineConfig, events: list) -> None:
    for event in events:
        if isinstance(event, CycleStarted):
            logger.info("%s: cycle started", cfg.id)
        elif isinstance(event, CameOnline):
            logger.info("%s: sensor online (gap %.0fs)", cfg.id, event.gap_s)
        elif isinstance(event, WentOffline):
            # Loud when it matters: a sensor that dropped off mid-cycle is the
            # one case where the text will be late, and this line is the only
            # warning of it.
            (logger.warning if event.was_running else logger.info)(
                "%s: sensor offline%s", cfg.id,
                " DURING A CYCLE — the cycle stays open and the text will be "
                "delayed until it reports again" if event.was_running else "",
            )
        elif isinstance(event, FalseStart):
            logger.info(
                "%s: discarded a %.0fs run as too short to be a load "
                "(min_run_seconds=%.0f)",
                cfg.id, event.ended_at - event.started_at, cfg.min_run_seconds,
            )
            db.record_cycle(cfg.id, event.started_at, event.ended_at, 0.0,
                            false_start=True)
        elif isinstance(event, CycleFinished):
            _finish(cfg, event)


def _finish(cfg: MachineConfig, event: CycleFinished) -> None:
    """Record the cycle, then text — in that order, and only once.

    record_cycle returns None when the row already existed, which is the guard
    against texting twice for one load. Writing first also means a crash between
    the two leaves evidence that the cycle happened, rather than a text with
    nothing behind it.
    """
    cycle_id = db.record_cycle(
        cfg.id, event.started_at, event.ended_at, event.peak_mg
    )
    if cycle_id is None:
        logger.warning(
            "%s: cycle starting %.0f already recorded — not texting again",
            cfg.id, event.started_at,
        )
        return

    logger.info(
        "%s: CYCLE COMPLETE after %.0fs (peak %.0f mg) — texting",
        cfg.id, event.duration_s, event.peak_mg,
    )
    ok, error = notify.send(cfg, event.started_at, event.ended_at)
    db.mark_notified(cycle_id, ok, error)
    if not ok:
        logger.error("%s: text FAILED: %s", cfg.id, error)


def record_sample(
    device: str,
    rms_mg: float,
    peak_mg: float | None = None,
    *,
    window_ms: int | None = None,
    seq: int | None = None,
    rssi: int | None = None,
    temp_c: float | None = None,
    now: float | None = None,
) -> MachineState:
    """Ingest one reading. Returns the machine's state after folding it in."""
    cfg = MACHINES[device]
    st = STATES[device]
    ts = time.time() if now is None else now

    db.insert_sample(device, ts, rms_mg, peak_mg if peak_mg is not None else rms_mg,
                     window_ms, seq, rssi, temp_c)
    events = observe(cfg, st, ts, rms_mg, peak_mg)
    db.save_state(st)
    _handle_events(cfg, events)
    return st


async def sweep_forever() -> None:
    """Liveness only. Cannot end a cycle — see detect.tick."""
    while True:
        try:
            now = time.time()
            for device, cfg in MACHINES.items():
                st = STATES[device]
                events = tick(cfg, st, now)
                if events:
                    db.save_state(st)
                    _handle_events(cfg, events)
        except Exception:  # noqa: BLE001 — the sweep must outlive any one error
            logger.exception("liveness sweep failed")
        await asyncio.sleep(config.TICK_SECONDS)


async def prune_forever() -> None:
    while True:
        try:
            removed = db.prune_samples(config.SAMPLE_RETENTION_DAYS)
            if removed:
                logger.info("pruned %d samples older than %.1f days",
                            removed, config.SAMPLE_RETENTION_DAYS)
        except Exception:  # noqa: BLE001
            logger.exception("sample prune failed")
        await asyncio.sleep(3600)
