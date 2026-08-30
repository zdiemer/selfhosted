"""Prometheus exposition, hand-written.

No prometheus_client: this is eleven series and forty lines, and the repo's
habit is to not carry a dependency for that (see life/carson's requirements.txt
on the absent ICS library). infra/alloy scrapes pods that ask for it.

These series are how `threshold_mg` actually gets chosen. `laundry_vibration_mg`
plotted over a full wash shows the idle floor, the agitate plateau, the spin
spike and — the one that matters — how long the soak pause really is, which is
the number `quiet_seconds` has to clear.
"""

from __future__ import annotations

import time

from detect import RUNNING, MachineConfig, MachineState


def _line(name: str, labels: dict[str, str], value: float) -> str:
    rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f"{name}{{{rendered}}} {value}"


def render(
    machines: dict[str, MachineConfig],
    states: dict[str, MachineState],
    cycle_counts: dict[str, int],
) -> str:
    now = time.time()
    out: list[str] = [
        "# HELP laundry_vibration_mg Last reported vibration RMS in milli-g.",
        "# TYPE laundry_vibration_mg gauge",
    ]
    for device, st in states.items():
        out.append(_line("laundry_vibration_mg", {"device": device}, st.last_rms_mg))

    out += [
        "# HELP laundry_threshold_mg Configured vibration threshold in milli-g.",
        "# TYPE laundry_threshold_mg gauge",
    ]
    for device, cfg in machines.items():
        out.append(_line("laundry_threshold_mg", {"device": device}, cfg.threshold_mg))

    out += [
        "# HELP laundry_machine_running 1 while a cycle is in progress.",
        "# TYPE laundry_machine_running gauge",
    ]
    for device, st in states.items():
        out.append(_line("laundry_machine_running", {"device": device},
                         1 if st.state == RUNNING else 0))

    out += [
        "# HELP laundry_device_online 1 while the sensor is reporting.",
        "# TYPE laundry_device_online gauge",
    ]
    for device, st in states.items():
        out.append(_line("laundry_device_online", {"device": device},
                         1 if st.online else 0))

    out += [
        "# HELP laundry_last_sample_age_seconds Age of the most recent reading.",
        "# TYPE laundry_last_sample_age_seconds gauge",
    ]
    for device, st in states.items():
        # -1, not 0 and not absent: a device that has never reported is a
        # different thing from one that reported this instant, and a gap in the
        # series would silently break an alert written as a threshold on age.
        age = (now - st.last_sample_at) if st.last_sample_at else -1
        out.append(_line("laundry_last_sample_age_seconds", {"device": device}, age))

    out += [
        "# HELP laundry_cycle_elapsed_seconds Elapsed time of the open cycle, 0 when idle.",
        "# TYPE laundry_cycle_elapsed_seconds gauge",
    ]
    for device, st in states.items():
        elapsed = (now - st.cycle_started_at) if st.cycle_started_at else 0
        out.append(_line("laundry_cycle_elapsed_seconds", {"device": device}, elapsed))

    out += [
        "# HELP laundry_cycles_total Completed cycles recorded (excludes false starts).",
        "# TYPE laundry_cycles_total counter",
    ]
    for device, count in cycle_counts.items():
        out.append(_line("laundry_cycles_total", {"device": device}, count))

    return "\n".join(out) + "\n"
