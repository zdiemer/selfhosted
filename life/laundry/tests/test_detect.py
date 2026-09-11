"""The properties that decide whether the texts are trustworthy.

Run:  python3 -m pytest life/laundry/tests -q

These are all pure-function tests over synthetic timestamps, so a five-hour
laundry day runs in about a millisecond. The one worth reading is
`test_sensor_dropout_does_not_finish_a_cycle` — that is the bug this design
exists to prevent.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from detect import (  # noqa: E402
    IDLE,
    RUNNING,
    CycleFinished,
    CycleStarted,
    FalseStart,
    MachineConfig,
    MachineState,
    WentOffline,
    format_duration,
    observe,
    tick,
)

CFG = MachineConfig(
    id="washer", label="Washer",
    threshold_mg=20.0,
    start_seconds=60.0,
    quiet_seconds=300.0,
    min_run_seconds=600.0,
    offline_seconds=120.0,
)

STEP = 5.0  # the firmware's post interval


def feed(st: MachineState, start: float, seconds: float, rms: float,
         cfg: MachineConfig = CFG) -> tuple[float, list]:
    """Post `rms` every STEP seconds for `seconds`. Returns (next_ts, events)."""
    events: list = []
    t = start
    end = start + seconds
    while t < end:
        events += observe(cfg, st, t, rms, rms)
        t += STEP
    return t, events


def test_a_bump_does_not_start_a_cycle():
    st = MachineState(device="washer")
    # 30 s of shaking — someone loading it — then stillness.
    t, events = feed(st, 1000.0, 30, 500.0)
    assert not any(isinstance(e, CycleStarted) for e in events)
    assert st.state == IDLE


def test_normal_cycle_starts_finishes_and_reports_true_duration():
    st = MachineState(device="washer")
    t0 = 1000.0
    t, _ = feed(st, t0, 2400, 180.0)          # 40 min of washing
    assert st.state == RUNNING
    assert st.cycle_started_at == t0          # backdated to the first reading

    t, events = feed(st, t, 400, 1.0)         # then still
    finished = [e for e in events if isinstance(e, CycleFinished)]
    assert len(finished) == 1
    f = finished[0]
    # Ends when it WENT still, not when the timer expired: ~2400 s, not ~2700.
    assert abs(f.duration_s - 2400) <= STEP
    assert st.state == IDLE


def test_a_soak_pause_shorter_than_quiet_seconds_does_not_end_the_cycle():
    """The failure this parameter exists for: washers stop for minutes mid-load."""
    st = MachineState(device="washer")
    t = 1000.0
    t, _ = feed(st, t, 900, 180.0)            # agitate
    t, events = feed(st, t, 240, 2.0)         # 4 min soak — under quiet_seconds
    assert not any(isinstance(e, CycleFinished) for e in events)
    assert st.state == RUNNING

    t, _ = feed(st, t, 900, 400.0)            # spin
    assert st.state == RUNNING
    t, events = feed(st, t, 400, 1.0)         # really done now
    assert any(isinstance(e, CycleFinished) for e in events)


def test_sensor_dropout_does_not_finish_a_cycle():
    """Silence from the ESP32 must never be read as silence from the machine.

    The wifi drops for 20 minutes mid-wash — far longer than quiet_seconds — and
    the machine keeps running the whole time. Nothing may be reported.
    """
    st = MachineState(device="washer")
    t = 1000.0
    t, _ = feed(st, t, 900, 180.0)
    assert st.state == RUNNING

    # 20 minutes of nothing. Only the clock moves; no samples arrive.
    now = t + 1200
    events = tick(CFG, st, now)
    assert any(isinstance(e, WentOffline) and e.was_running for e in events)
    assert st.state == RUNNING, "a cycle must not end while we are blind"

    # It comes back, still shaking. Cycle continues, same cycle.
    started = st.cycle_started_at
    t, events = feed(st, now, 300, 180.0)
    assert st.state == RUNNING
    assert st.cycle_started_at == started
    assert not any(isinstance(e, CycleFinished) for e in events)


def test_quiet_clock_restarts_after_a_blind_gap():
    """Coming back to a still machine cannot credit the gap as quiet time."""
    st = MachineState(device="washer")
    t = 1000.0
    t, _ = feed(st, t, 900, 180.0)
    assert st.state == RUNNING

    # Gone for 10 min, and the first reading back is below threshold. If the gap
    # counted, that single sample would immediately satisfy quiet_seconds.
    resumed = t + 600
    events = observe(CFG, st, resumed, 1.0, 1.0)
    assert not any(isinstance(e, CycleFinished) for e in events)
    # The clock is seeded at the RECONNECT, never backdated into the gap — had
    # it been backdated, this one reading would have satisfied quiet_seconds
    # outright and texted while the machine was still going.
    assert st.quiet_since == resumed

    # Now it genuinely sits still for the full window, from the reconnect.
    t, events = feed(st, resumed + STEP, 400, 1.0)
    assert any(isinstance(e, CycleFinished) for e in events)


def test_run_shorter_than_min_run_is_a_false_start_and_never_texts():
    st = MachineState(device="washer")
    t = 1000.0
    t, _ = feed(st, t, 300, 300.0)            # 5 min: a spin-only nudge
    assert st.state == RUNNING                # it did start...
    t, events = feed(st, t, 400, 1.0)
    assert any(isinstance(e, FalseStart) for e in events)
    assert not any(isinstance(e, CycleFinished) for e in events)
    assert st.state == IDLE


def test_tick_alone_can_never_finish_a_cycle():
    """No amount of elapsed time decides anything. Only readings do."""
    st = MachineState(device="washer")
    t, _ = feed(st, 1000.0, 900, 180.0)
    assert st.state == RUNNING
    for hour in range(1, 25):
        assert not any(isinstance(e, CycleFinished)
                       for e in tick(CFG, st, t + hour * 3600))
    assert st.state == RUNNING


def test_readings_at_the_threshold_count_as_running():
    st = MachineState(device="washer")
    t, _ = feed(st, 1000.0, 120, CFG.threshold_mg)
    assert st.state == RUNNING


def test_back_to_back_loads_are_two_cycles():
    """Consecutive loads must each get their own text — see notify.idempotency_key."""
    st = MachineState(device="washer")
    t = 1000.0
    t, _ = feed(st, t, 1800, 180.0)
    t, first = feed(st, t, 400, 1.0)
    t, _ = feed(st, t, 1800, 180.0)
    t, second = feed(st, t, 400, 1.0)

    a = [e for e in first if isinstance(e, CycleFinished)]
    b = [e for e in second if isinstance(e, CycleFinished)]
    assert len(a) == 1 and len(b) == 1
    assert a[0].started_at != b[0].started_at


def test_format_duration():
    assert format_duration(40) == "40 s"
    assert format_duration(2820) == "47 min"
    assert format_duration(6420) == "1 h 47 min"
    assert format_duration(7200) == "2 h"
