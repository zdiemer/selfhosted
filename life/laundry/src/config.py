"""Runtime configuration, all of it from the environment.

The machine list is a JSON blob rather than a pile of LAUNDRY_WASHER_* variables
because it is a LIST — one entry today, two the moment the second sensor is
mounted, and the chart builds it from `machines:` in values.yaml. Tuning a
threshold is then a `helm upgrade`, not a reflash of something glued to the back
of an appliance, which is the entire reason the detection logic lives up here
and not on the ESP32.
"""

from __future__ import annotations

import json
import logging
import os

from detect import MachineConfig

logger = logging.getLogger("laundry.config")

DB_PATH = os.environ.get("LAUNDRY_DB", "/data/laundry.db")
HTTP_PORT = int(os.environ.get("LAUNDRY_HTTP_PORT", "8080"))
LOG_LEVEL = os.environ.get("LAUNDRY_LOG_LEVEL", "INFO")

# What the ESP32 presents on every POST. One token for both machines: they are
# the same two devices in the same house, a leak means reflashing both anyway,
# and the ingest listener is LAN-only (a LoadBalancer on the node IPs, never
# published through Traefik). Rotating it is a helm upgrade plus a reflash.
INGEST_TOKEN = os.environ.get("LAUNDRY_INGEST_TOKEN", "")

SMS_URL = os.environ.get("LAUNDRY_SMS_URL", "http://sms-relay.infra.svc.cluster.local:8000")
SMS_API_KEY = os.environ.get("LAUNDRY_SMS_API_KEY", "")
SMS_TO = os.environ.get("LAUNDRY_SMS_TO", "")
DRY_RUN = os.environ.get("LAUNDRY_DRY_RUN", "") == "1"

# Raw readings are kept only to draw the tuning trace on the dashboard and in
# Grafana. At one row per 5 s per machine that is ~35k rows/week/machine — small,
# but unbounded, and this volume is 1Gi.
SAMPLE_RETENTION_DAYS = float(os.environ.get("LAUNDRY_SAMPLE_RETENTION_DAYS", "7"))

# How often the liveness sweep runs. Only ever marks a device offline; see
# detect.tick for why it can never decide a cycle is over.
TICK_SECONDS = float(os.environ.get("LAUNDRY_TICK_SECONDS", "10"))


def load_machines() -> dict[str, MachineConfig]:
    raw = os.environ.get("LAUNDRY_MACHINES", "").strip()
    if not raw:
        # An empty list is a service that accepts nothing and texts nobody. That
        # is a silent failure of the only job it has, so refuse to start instead.
        raise SystemExit(
            "LAUNDRY_MACHINES is empty — set `machines:` in values.yaml. "
            "With no machines configured nothing is monitored and no text is "
            "ever sent, which looks identical to a quiet laundry room."
        )
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"LAUNDRY_MACHINES is not valid JSON: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise SystemExit("LAUNDRY_MACHINES must be a non-empty JSON array")

    machines: dict[str, MachineConfig] = {}
    for entry in entries:
        cfg = MachineConfig.from_dict(entry)
        if cfg.id in machines:
            raise SystemExit(f"duplicate machine id {cfg.id!r} in LAUNDRY_MACHINES")
        if cfg.quiet_seconds <= 0 or cfg.start_seconds <= 0:
            raise SystemExit(f"{cfg.id}: start_seconds and quiet_seconds must be > 0")
        machines[cfg.id] = cfg
        logger.info(
            "machine %s (%s): threshold=%.1fmg start=%.0fs quiet=%.0fs min_run=%.0fs",
            cfg.id, cfg.label, cfg.threshold_mg, cfg.start_seconds,
            cfg.quiet_seconds, cfg.min_run_seconds,
        )
    return machines
