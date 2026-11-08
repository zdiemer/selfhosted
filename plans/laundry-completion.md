# Finish Laundry setup

Status: backend deployed; washer sensor wired and the firmware configured and
building, but not yet flashed. Blocked on soldering the GY-521 header.

## Existing work

The [Laundry chart, firmware and setup guide](../life/laundry/README.md) cover
ESP32/MPU-6050 sampling, authenticated LAN ingest, persisted cycle detection,
the dashboard and notification delivery through sms-relay. The live backend
has one configured washer, an offline sensor whose last sample is over 70 days
old, and no recorded cycles at this review. A healthy Deployment is not a
working appliance monitor. The old sample is not evidence of a previously
commissioned physical sensor.

## Progress (2026-11-08)

Verified rather than assumed:

- `secrets.smsApiKey`, `secrets.ingestToken` and `sms.to` exist in the vault, and
  the live sms-relay `SMS_RELAY_API_KEYS` contains a `laundry` entry.
- `firmware/include/config.h` exists (gitignored, never committed). Its ingest
  token matches the live `LAUNDRY_INGEST_TOKEN` (compared by hash), it targets
  `192.168.4.26:8420`, and Wi-Fi is set to the house 2.4 GHz network (the
  ESP32 cannot join the 5 GHz one).
- PlatformIO Core 6.2.0 is installed in `~/.local/share/platformio-venv`, linked
  as `~/.local/bin/pio`. `pio run` builds cleanly (flash 71.5%).
- The owner was added to `dialout`. Until the next login, run serial commands
  under `sg dialout -c "…"`.
- GY-521 is wired to the ESP32-D: VCC→3V3, GND→GND, SCL→P22, SDA→P21, AD0
  unconnected. The board's `P` labels are GPIO numbers.

## Remaining work

- [ ] Solder the GY-521 header strip (short pins through the board; all eight
  pins, no bridges). It is press-fit only. An unsoldered header gives
  `[mpu] no reply` or intermittent readings, and hot glue does not substitute.
  Unplug the jumpers before soldering and reconnect them as listed above.
- [ ] Plug in over a data-capable USB cable, confirm `/dev/ttyUSB0` (or
  `ttyACM0`) appears, then flash and watch:
  ```bash
  cd ~/Code/selfhosted/life/laundry/firmware
  sg dialout -c "$HOME/.local/bin/pio run -t upload && $HOME/.local/bin/pio device monitor"
  ```
  Hold BOOT if the upload stalls at `Connecting....`. Expect `WHO_AM_I = 0x68`
  (0x70/0x72/0x73 also fine), a `[wifi] 192.168.4.x` line, then
  `[post] washer … -> idle`. Confirm in `kubectl -n life logs -f deploy/laundry`
  and `laundry_device_online{device="washer"} 1`.
- [ ] With explicit permission to send a real text, run
  `POST /api/v1/test-notify?device=washer` through a port-forward. Verify the
  text is actually received and check its delivery status, not just that the
  relay queued it.
- [ ] Buy a ~15–20 mm neodymium magnet (check the panel is steel first). Mount
  on the top panel toward the rear or the upper side panel, with the ESP32 off
  the machine. No foam tape.
- [ ] Record a full real washer cycle, including its longest quiet pause. Tune
  server-side thresholds and timers from that trace, not the example readings.
- [ ] After calibration, strain-relieve the jumpers: secure the connectors with
  hot glue or heat-shrink at both ends, bundle the wires, leave a slack loop and
  anchor the bundle to the machine. Solder them permanently if wanted.
- [ ] Verify sensor dropout never produces a false completion, and reconnecting
  resumes detection without creating duplicate cycles.
- [ ] Complete another real load without an early or duplicate notification.
- [ ] Optionally enable `ingress.enabled` for `laundry.zachd.duckdns.org`.
- [ ] Confirm whether dryer coverage is wanted now; if so, add a sensor and
  independently calibrate it rather than assuming the washer settings apply.

Done when the intended appliances report fresh samples and complete real cycles
with one correctly timed, received notification each. Put calibrated settings
and lasting operating instructions in the service directory, then remove this plan.
