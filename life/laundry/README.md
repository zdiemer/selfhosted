# laundry — the washer and dryer tell you when they're done

An ESP32 with an MPU-6050 accelerometer glued to each machine measures how hard
it is shaking and posts that number to the cluster every five seconds. The
cluster decides when a load has finished and sends a text through
[`infra/sms-relay`](../../infra/sms-relay/).

Dashboard at **`laundry.zachd.duckdns.org`** (Authelia). Sensors talk to a
LoadBalancer on the node IPs, not to any hostname — see
[The network problem](#the-network-problem).

```
  MPU-6050 ──I²C──┐
   (on the washer)  │      every 5 s
                    ├─ ESP32 ──HTTP──> :8420 on any node IP
  MPU-6050 ──I²C──┘   (wifi)            │   {"device":"washer","rms_mg":182.4}
   (on the dryer)                       │
                                        ▼
                          laundry pod ── state machine per machine
                                        │   idle → running → done
                                        ▼
                                 infra/sms-relay ──> Android handset ──> 📱
```

## Why the ESP32 decides nothing

The device measures. Every threshold and every timer lives in the chart. That
split is the whole design, and it is there for one reason: **you cannot know the
right threshold until you have watched a real load, and by then the sensor is
stuck to the back of a washing machine.** Tuning has to be a `helm upgrade`, not
a reflash. The rest follows from that — the device holds a low-value ingest
token instead of the SMS key, two machines share one configuration, and the
vibration trace is on a dashboard where you can actually look at it.

## The measurement

The accelerometer is sampled at 100 Hz and each 5-second window is reduced to
the **standard deviation of the acceleration magnitude, in milli-g**.

Taking the deviation rather than the level is what subtracts gravity — and with
it, every dependence on how the board happens to be oriented. A sensor flat on
the lid and one sideways and upside down on the back panel read the same for the
same shaking. That is why the mounting instructions below can be relaxed about
which way up it goes.

| | typical reading |
|---|---|
| The sensor's own noise floor | ~3 mg |
| Idle appliance, room quiet | 2–8 mg |
| Washer filling / agitating | 30–200 mg |
| Washer on spin | 200–900 mg |
| Dryer tumbling | 40–300 mg |

Those are starting points, not gospel. Yours depend on the machine, the floor
and where you stick the thing — which is what the calibration step is for.

## The decision

Four numbers per machine, all in `values.yaml`:

| | what it does | why it exists |
|---|---|---|
| `thresholdMg` | above this = "moving" | separates the appliance from the sensor's noise |
| `startSeconds` | vibration must persist this long to start a cycle | rejects the door slam and loading the drum |
| `quietSeconds` | stillness must persist this long to end one | **a washer's soak pause is several genuine minutes of stillness in the middle of a load** |
| `minRunSeconds` | shorter than this was never a load | rejects a spin-only nudge; recorded as a false start, never texted |

`quietSeconds` is the one that matters. Too low and you get a text halfway
through the wash; too high and the text is merely late. It ships at 7 minutes
for the washer and should be *lowered* from the trace, not raised into place.

### The rule that keeps the texts trustworthy

**Silence from the sensor is not silence from the machine.** A finished washer
and an ESP32 that fell off the wifi produce exactly the same thing at this
layer: no samples. Getting that backwards is the failure that costs something —
a text saying the load is done, sent while it is still spinning, teaches you to
stop believing the texts.

So quiet is only ever accumulated from readings that *arrived*. Elapsed time on
its own can never end a cycle; if the device disappears mid-load the cycle stays
open, and when it comes back the quiet clock restarts from the reconnect. The
worst case is a late text. There is no case that produces an early one.

That property is pinned down in [`tests/test_detect.py`](tests/test_detect.py) —
`test_sensor_dropout_does_not_finish_a_cycle` and
`test_tick_alone_can_never_finish_a_cycle` are the two worth reading.

```bash
python3 -m pytest life/laundry/tests -q
```

## The network problem

The ESP32 posts to **a node's LAN IP on `:8420`**, not to
`laundry.zachd.duckdns.org`. That is forced, not a shortcut:

Every `*.zachd.duckdns.org` name in this repo resolves to a **Tailscale**
address (`100.x`), and the cluster advertises no subnet routes. A laptop
resolves it because the laptop is on the tailnet. A microcontroller cannot run
`tailscaled`, so for this device the hostname simply does not resolve to
anything it can reach.

What does work is `klipper-lb`: the `laundry-ingest` LoadBalancer binds `:8420`
on **every** node's LAN address and forwards to the pod wherever it is running.
So the firmware names one node IP without being pinned to that node — the same
arrangement `minecraft` and `games/ffxiv-1x` use, and for the same reason: the
client needs an IP.

| node | IP | | node | IP |
|---|---|---|---|---|
| zachd-ubuntu | `192.168.4.26` | | zachd-ubuntu-3 | `192.168.4.25` |
| zachd-ubuntu-1 | `192.168.4.28` | | zachd-ubuntu-4 | `192.168.4.27` |
| zachd-ubuntu-2 | `192.168.4.37` | | zachd-ubuntu-5 | `192.168.4.21` |

Prefer a control-plane node (`.26`, `.25`, `.21`) — least likely to be drained.

**Two listeners, two ports.** The pod serves the dashboard on `:8080` (ClusterIP
→ Traefik → Authelia) and device ingest on `:8081`, and only the second is
published to the LAN. What sits on an unauthenticated LAN port is therefore
exactly one route that accepts a vibration reading — not the dashboard, and not
the cycle log, which is a record of when somebody is home doing laundry.

---

# Building the thing

## Parts

| | |
|---|---|
| ESP32-D devkit (WROOM-32, wifi) | ✅ have |
| GY-521 / MPU-6050 breakout | ✅ have one |
| 4 × female–female jumper wires | ✅ have |
| USB power supply + cable | phone charger is fine |
| A small neodymium magnet (~15–20 mm) | see [Mounting](#mounting) — worth buying |

## Wiring

Four wires. The GY-521's other pins (`XDA`, `XCL`, `INT`) stay unconnected.

| GY-521 | → | ESP32 |
|---|---|---|
| `VCC` | → | `3V3` |
| `GND` | → | `GND` |
| `SCL` | → | `GPIO22` |
| `SDA` | → | `GPIO21` |
| `AD0` | → | *leave unconnected* (= address `0x68`) |

**Power it from `3V3`, not `VIN`/`5V`.** The GY-521 has an onboard regulator so
5 V won't hurt the chip — but on many of the clone boards the I²C pull-up
resistors are tied to the input rail, which would put ~5 V on `SDA`/`SCL` and
into GPIO pins rated for 3.3 V. Running the whole board at 3.3 V means the
question never arises. It draws about 4 mA; the ESP32's regulator won't notice.

## Flashing

```bash
cd life/laundry/firmware
cp include/config.example.h include/config.h   # gitignored
$EDITOR include/config.h                       # wifi, node IP, ingest token, machine id
pio run -t upload
pio device monitor                             # 115200 baud
```

`include/config.h` holds the wifi password and the ingest token, so it is
gitignored the same way every `values.local.yaml` in this repo is.

Within a few seconds the monitor should show:

```
[mpu] 0x68: WHO_AM_I = 0x68
[mpu] washer @ 0x68: ok
[wifi] 192.168.4.83  rssi -58 dBm
[post] washer rms=3.4 mg peak=8.1 mg -> idle
```

`WHO_AM_I` answering `0x70`/`0x72`/`0x73` instead of `0x68` is fine — those are
the MPU-6052/9250 clones sold as GY-521 and they are register-compatible here.
No reply at all means the wiring: check `3V3`, `GND`, and that `SDA`/`SCL`
aren't swapped.

The onboard LED is your only feedback once this is behind an appliance:
**flickering** = joining wifi, **lit** = the server says this machine is
running, **dark** = idle.

## Mounting

The sensor has to be **rigidly** coupled to the machine's shell. This is the one
place where the obvious choice is wrong:

> **Don't use foam mounting tape or Command strips.** Foam is designed to absorb
> vibration. It is a mechanical low-pass filter placed directly over the signal,
> and it will flatten a running washer down toward the noise floor.

**Use a magnet.** Washer and dryer shells are steel. Hot-glue or epoxy the
GY-521 to a small neodymium magnet and stick it on. It is rigid, it is
removable, and — the part that actually matters — you can *move it* while
calibrating, which you will want to do. Check with a fridge magnet first: some
front panels are non-magnetic stainless, though the side and top panels are
almost always plain steel.

Failing that, thin VHB double-sided tape or a zip tie around an existing
bracket. Both are rigid. Foam is not.

**Where on the machine:**

- ✅ A large flat panel that can resonate — the **top panel toward the rear**,
  or the **upper side panel**. These move the most.
- ❌ The frame or base. Very stiff, and the signal there is weak.
- ❌ The control console. Often isolated from the drum on purpose.
- ❌ The door or lid. Moves independently, and gets opened.
- ❌ Anywhere on a dryer that gets hot to the touch.

**Which way up doesn't matter.** Gravity is subtracted out. Mount it whichever
way the magnet and the wires want to sit.

**The ESP32 itself** should sit *off* the machine where you can — a shelf, the
wall, the side of a cabinet — with the jumper wires running to the sensor. Two
reasons: a dryer top gets warm enough over years to matter to the electrolytics,
and the ESP32's antenna wants to be clear of large metal surfaces. Don't wedge
it into the gap between two appliances; laundry rooms already tend to be the
worst wifi in the house. The firmware reports RSSI on every sample, so you can
check rather than guess.

**Strain-relieve the jumpers.** Dupont connectors on header pins genuinely do
work themselves loose under sustained vibration — this is the most likely way
the whole thing quietly stops working in three months. A dab of hot glue over
each connector, or tape the bundle down so the machine isn't tugging the plugs.
Solder them if you want it permanent.

## One ESP32 or two?

You have one MPU-6050 today, so start with the washer — it is the machine whose
"done" is least obvious from across the house, and the one with the awkward soak
pause worth tuning against.

For the dryer, **you may not need a second ESP32.** Two MPU-6050s can share one
board on the same I²C bus:

- Sensor 1: `AD0` unconnected → address `0x68`
- Sensor 2: `AD0` tied to `3V3` → address `0x69`

Both share `SDA`, `SCL`, `3V3` and `GND`. Uncomment `SENSOR_2_DEVICE` in
`config.h`, uncomment the `dryer` block in `values.yaml`, and one board covers
both machines.

That works if the machines are side by side — the bus runs at 100 kHz precisely
so a metre or so of jumper wire stays reliable. If they are further apart than
that, or on opposite walls, buy a second ESP32 (~$5) and flash the same firmware
with `SENSOR_1_DEVICE "dryer"`. Two boards is also the more robust answer: one
falling off the wifi doesn't blind you to both machines.

Either way the cluster side is identical — a second entry in `machines:`.

---

# Deploying

```bash
kubectl create namespace life          # if it doesn't exist
./build.sh                             # → ghcr.io/zdiemer/laundry:vN
./upgrade.sh
```

Secrets live at `op://homelab/life-laundry/values.local.yaml` and are resolved
into memory at deploy time — see [`values.local.yaml.example`](values.local.yaml.example)
for the shape. Three things:

1. `secrets.smsApiKey` — add `laundry` to sms-relay's `apiKeys` map first, then
   paste the key it issues.
2. `secrets.ingestToken` — `openssl rand -hex 32`. Must match
   `LAUNDRY_INGEST_TOKEN` in the firmware's `config.h`.
3. `sms.to` — the recipient. A phone number, not a config; this repo is public.

Bring the pod up with `ingress.enabled: false` first, confirm the sensors are
reporting, then flip the ingress on.

Verify the SMS path end to end without doing a load — **this sends a real
text**:

```bash
kubectl -n life port-forward deploy/laundry 8080:8080
curl -X POST 'localhost:8080/api/v1/test-notify?device=washer'
```

Set `extraEnv.LAUNDRY_DRY_RUN=1` to log the message instead of sending it.

## Calibrating

Do this once, with a real load. It takes one wash and replaces all the guessing.

1. Open the dashboard (`kubectl port-forward`, or the ingress once it's on).
   It refreshes every 5 s and draws the last two hours.
2. **Machine off.** Read the idle floor — expect single-digit mg. If it reads
   30 mg with nothing running, the sensor is picking up the room; move it.
3. **Start a load.** Watch the trace fill in. Note the running level, and note
   the **longest dip inside the cycle** — that is the soak/drain pause, and it
   is the number `quietSeconds` has to clear.
4. Set the values and roll them out — no rebuild, no reflash:

   ```bash
   ./upgrade.sh --set machines[0].thresholdMg=25 --set machines[0].quietSeconds=480
   ```

   (Then write them into `values.yaml` so they survive the next deploy.)

Rules of thumb: put `thresholdMg` about 4–5× the idle floor and comfortably
below the running level; set `quietSeconds` to roughly twice the longest
in-cycle pause you observed.

If you get a text partway through a load, `quietSeconds` is too low. If the text
is consistently late by a fixed amount, it is higher than it needs to be — the
text already reports the *true* finish time, so lateness is only about how long
you wait to hear.

## Watching it

```bash
kubectl -n life logs -f deploy/laundry
```

```
washer: cycle started
washer: sensor offline DURING A CYCLE — the cycle stays open and the text will be delayed until it reports again
washer: CYCLE COMPLETE after 2700s (peak 840 mg) — texting
```

`/metrics` is scraped by [`infra/alloy`](../../infra/alloy/); plotting
`laundry_vibration_mg` against `laundry_threshold_mg` over a full load is the
same tuning picture as the dashboard, with history.

| series | |
|---|---|
| `laundry_vibration_mg` | last reported RMS |
| `laundry_machine_running` | 1 during a cycle |
| `laundry_device_online` | 1 while the sensor reports |
| `laundry_last_sample_age_seconds` | −1 if it has never reported |
| `laundry_cycle_elapsed_seconds` | 0 when idle |
| `laundry_cycles_total` | completed loads, false starts excluded |

## When it doesn't work

| symptom | likely cause |
|---|---|
| `[mpu] 0x68: no reply` | wiring — `3V3`, `GND`, or `SDA`/`SCL` swapped |
| `HTTP 401` on every post | `LAUNDRY_INGEST_TOKEN` ≠ `secrets.ingestToken` |
| `HTTP 404` with a device list | flashed `SENSOR_n_DEVICE` isn't a machine `id` in `values.yaml` |
| Posts time out | wrong node IP, or `ingestService.enabled: false` |
| Idle reads 30+ mg | sensor is picking up the room or a loose mount; move it, re-seat it |
| Running reads under 20 mg | foam tape, or mounted on the frame instead of a panel |
| Text arrives mid-cycle | `quietSeconds` shorter than the soak pause — raise it |
| Load finishes, no text | check the cycle row: `false_start=1` → `minRunSeconds` too high; `notified=0` → `notify_error` has the sms-relay failure |
| Worked for weeks, then stopped | a jumper vibrated loose. See the strain-relief note above |

## Notes

- SQLite on a ReadWriteOnce PVC. `replicas: 1` and `strategy: Recreate` are
  load-bearing: two pods would be two SQLite writers *and* two copies of the
  state machine each seeing half the samples, so neither would ever accumulate
  enough quiet to call a load finished.
- Machine state is persisted after every reading, so a `helm upgrade` in the
  middle of a wash resumes the open cycle rather than forgetting it.
- Raw samples are pruned after `sampleRetentionDays` (7). The cycle log is kept
  forever — it's a few rows a week, and it's the only evidence the thing works.
- The idempotency key sent to sms-relay is scoped per cycle. A coarser key would
  send the first text ever and then silently swallow every one after it; see
  [`src/notify.py`](src/notify.py), which carries the same warning
  `life/carson` does, learned the same way.
- The firmware never buffers a failed post to replay later. The server treats
  arrival time as sample time — the ESP32 has no RTC — so a replayed window
  would be a lie about the present. A dropped post is dropped, and the cycle
  stays open across the gap.
- No STOP/HELP handling and no quiet hours. If this ever texts at 2 a.m. because
  a load finished at 2 a.m., that is working as designed.
