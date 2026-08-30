"""The state machine that turns a stream of vibration readings into "it's done".

Pure functions over a dataclass: no database, no clock, no network. Everything
here is driven by the timestamps handed in, which is what makes the whole thing
testable in milliseconds instead of in laundry loads (see ../tests/test_detect.py).

THE ONE RULE THAT MATTERS
-------------------------
**Silence from the sensor is not silence from the machine.** A washer that has
finished and an ESP32 that fell off the wifi produce exactly the same thing at
this layer: no samples. Getting that backwards is the only failure mode that
actually costs something — a text saying the load is done, sent while it is
still spinning, trains you to stop believing the texts.

So quiet is only ever accumulated from readings that ARRIVED. `observe()`
advances the quiet clock; `tick()` never does. If the device disappears
mid-cycle the cycle simply stays open, and when the device comes back the quiet
clock restarts from the reconnect (see the `gap` handling below). The worst case
is a text that arrives late. There is no case that produces an early one.

WHY THRESHOLDING RMS AND NOT ORIENTATION
----------------------------------------
The reading is the standard deviation of the accelerometer magnitude over a
window, in milli-g — the DC component (gravity, i.e. how the thing happens to be
stuck to the machine) is subtracted out on the device. That means mounting
orientation is irrelevant: upside down on the side panel reads the same as flat
on the lid. It is the one design choice that makes the physical install forgiving.

THE FOUR TIME CONSTANTS
-----------------------
`start_seconds`   how long vibration must persist before a cycle is real.
                  Rejects the door slam and the load being dumped in.
`quiet_seconds`   how long stillness must persist before the cycle is over.
                  THE tuning knob: a washer's soak pause is several minutes of
                  genuine stillness in the middle of a running cycle, and this
                  number has to be longer than the longest such pause.
`min_run_seconds` a cycle shorter than this was never a cycle. Rejects the
                  "spin only" nudge and someone leaning on the lid.
`offline_seconds` how long a gap makes the device untrustworthy rather than slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

# States. Deliberately only two — "done" is an event, not a state a machine
# sits in, and modelling it as one invites the question of when it ends.
IDLE = "idle"
RUNNING = "running"


@dataclass(frozen=True)
class MachineConfig:
    """One washer or one dryer. Comes from values.yaml via LAUNDRY_MACHINES."""

    id: str
    label: str
    # Milli-g of vibration that counts as "moving". The MPU-6050's own noise
    # floor is ~3 mg RMS over this bandwidth, so anything under about 10 is
    # thresholding on the sensor rather than on the appliance.
    threshold_mg: float = 20.0
    start_seconds: float = 60.0
    quiet_seconds: float = 300.0
    min_run_seconds: float = 600.0
    offline_seconds: float = 120.0
    # Body of the text. `{label}`, `{duration}` and `{finished}` are filled in.
    message: str = "{label} is done ({duration} cycle, finished {finished})."

    @staticmethod
    def from_dict(d: dict) -> "MachineConfig":
        """Build from one `machines:` entry in values.yaml.

        Keys arrive camelCase, because that is how every other chart in this
        repo writes values and a lone snake_case island would be the thing
        people get wrong. Both spellings are accepted; unknown keys are ignored
        rather than fatal, so a chart that grows a field ahead of the image
        still deploys.
        """
        def snake(key: str) -> str:
            return "".join(f"_{c.lower()}" if c.isupper() else c for c in key)

        fields = MachineConfig.__dataclass_fields__
        known = {snake(k): v for k, v in d.items() if snake(k) in fields}
        if "id" not in known:
            raise ValueError(f"machine config has no id: {d!r}")
        known.setdefault("label", str(known["id"]).replace("-", " ").title())
        return MachineConfig(**known)


@dataclass
class MachineState:
    """Everything the machine needs to remember between samples.

    Persisted to SQLite after every sample: a helm upgrade in the middle of a
    wash must not lose the open cycle, or the load finishes into a pod that
    thinks it was never running and nobody gets told.
    """

    device: str
    state: str = IDLE
    # When the current unbroken run of above-threshold readings began. Reset by
    # any quiet reading, so it measures a streak, not a total.
    active_since: float | None = None
    # Mirror image: when the current unbroken run of below-threshold readings
    # began. This is the one the "done" decision is made on.
    quiet_since: float | None = None
    cycle_started_at: float | None = None
    cycle_peak_mg: float = 0.0
    last_sample_at: float | None = None
    last_rms_mg: float = 0.0
    online: bool = False


# --------------------------------------------------------------------------
# Events the caller acts on. Returned rather than performed, so that sending a
# text is the API layer's problem and this file stays a pure function.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CycleStarted:
    device: str
    started_at: float


@dataclass(frozen=True)
class CycleFinished:
    """A real, completed load. THIS is the one that texts."""

    device: str
    started_at: float
    ended_at: float
    peak_mg: float

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at


@dataclass(frozen=True)
class FalseStart:
    """Ran, but not for long enough to have been a load. Recorded, never texted."""

    device: str
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class WentOffline:
    device: str
    last_sample_at: float | None
    was_running: bool


@dataclass(frozen=True)
class CameOnline:
    device: str
    gap_s: float


Event = CycleStarted | CycleFinished | FalseStart | WentOffline | CameOnline


def observe(
    cfg: MachineConfig,
    st: MachineState,
    ts: float,
    rms_mg: float,
    peak_mg: float | None = None,
) -> list[Event]:
    """Fold one reading into the state. Mutates `st`, returns what happened.

    `ts` is the SERVER's receipt time, not the device's. The ESP32 has no RTC
    and no NTP, so its own notion of time is milliseconds since boot — useful
    for spotting a reboot, useless for deciding when a cycle ended.
    """
    events: list[Event] = []
    peak_mg = rms_mg if peak_mg is None else peak_mg

    previous = st.last_sample_at
    gap = (ts - previous) if previous is not None else 0.0

    if not st.online:
        st.online = True
        events.append(CameOnline(cfg.id, gap))

    # A gap longer than offline_seconds means we were blind for that stretch.
    # Neither streak may span it: the machine could have started, stopped, or
    # both while we weren't looking. Restarting both clocks costs at most one
    # extra `quiet_seconds` of delay on the text and removes the entire class
    # of "the wifi dropped, so we assumed it went quiet" bug.
    if previous is not None and gap > cfg.offline_seconds:
        st.active_since = None
        st.quiet_since = None

    st.last_sample_at = ts
    st.last_rms_mg = rms_mg

    active = rms_mg >= cfg.threshold_mg

    if st.state == IDLE:
        if active:
            if st.active_since is None:
                st.active_since = ts
            if ts - st.active_since >= cfg.start_seconds:
                # The cycle is backdated to the first reading of the streak, not
                # to now — the load has been running for start_seconds already,
                # and min_run_seconds is measured against the real elapsed time.
                st.state = RUNNING
                st.cycle_started_at = st.active_since
                st.cycle_peak_mg = peak_mg
                st.quiet_since = None
                st.active_since = None
                events.append(CycleStarted(cfg.id, st.cycle_started_at))
        else:
            st.active_since = None
        return events

    # RUNNING
    st.cycle_peak_mg = max(st.cycle_peak_mg, peak_mg)
    if active:
        st.quiet_since = None
        return events

    if st.quiet_since is None:
        st.quiet_since = ts
    if ts - st.quiet_since < cfg.quiet_seconds:
        return events

    # Settled. The cycle ended when the stillness BEGAN, not when we finally
    # became confident about it — otherwise every recorded duration is inflated
    # by quiet_seconds and the text tells you the wrong finish time.
    started = st.cycle_started_at if st.cycle_started_at is not None else st.quiet_since
    ended = st.quiet_since
    if ended - started >= cfg.min_run_seconds:
        events.append(CycleFinished(cfg.id, started, ended, st.cycle_peak_mg))
    else:
        events.append(FalseStart(cfg.id, started, ended))

    st.state = IDLE
    st.cycle_started_at = None
    st.cycle_peak_mg = 0.0
    st.quiet_since = None
    st.active_since = None
    return events


def tick(cfg: MachineConfig, st: MachineState, now: float) -> list[Event]:
    """Time passing on its own. Only ever demotes liveness — never decides.

    Note what is NOT here: any advancing of `quiet_since`. That is the whole
    point. A machine whose sensor has gone silent stays `running` forever until
    the sensor comes back and says otherwise.
    """
    if st.last_sample_at is None:
        return []
    if st.online and (now - st.last_sample_at) > cfg.offline_seconds:
        st.online = False
        return [WentOffline(cfg.id, st.last_sample_at, st.state == RUNNING)]
    return []


def format_duration(seconds: float) -> str:
    """"1 h 47 min" / "47 min" / "40 s". Goes in the text, so it reads aloud."""
    seconds = max(0.0, float(seconds))
    # Seconds first, and on the raw value: rounding to minutes up front turns
    # 40 s into "1 min", which is the wrong word for a false start.
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"
