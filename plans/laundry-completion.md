# Finish Laundry setup

Status: backend deployed; the owner confirms the sensor has not been wired yet.
Wiring, firmware setup, mounting, calibration and end-to-end validation remain.

## Existing work

The [Laundry chart, firmware and setup guide](../life/laundry/README.md) cover
ESP32/MPU-6050 sampling, authenticated LAN ingest, persisted cycle detection,
the dashboard and notification delivery through sms-relay. The live backend
has one configured washer, an offline sensor whose last sample is over 53 days
old, and no recorded cycles at this review. A healthy Deployment is not a
working appliance monitor. The old sample is not evidence of a previously
commissioned physical sensor.

## Remaining work

- [ ] Confirm the available board/sensor hardware, then wire the MPU-6050 to the
  ESP32 following the existing guide. The physical wiring has not been done.
- [ ] Configure and flash the washer sensor using the existing guide. Keep Wi-Fi
  credentials and the ingest token out of Git; use the node LAN ingest endpoint.
- [ ] Mount and strain-relieve the sensor; verify fresh samples and sensible idle readings.
- [ ] Record a full real washer cycle, including its longest quiet pause. Tune
  server-side thresholds and timers from that trace, not the example readings.
- [ ] Verify sensor dropout never produces a false completion, and reconnecting
  resumes detection without creating duplicate cycles.
- [ ] Check the live relay route and intended recipient. With explicit permission
  for a real notification, verify receipt and delivery status, not just queue acceptance.
- [ ] Complete another real load without an early or duplicate notification.
- [ ] Confirm whether dryer coverage is wanted now; if so, add a sensor and
  independently calibrate it rather than assuming the washer settings apply.

Done when the intended appliances report fresh samples and complete real cycles
with one correctly timed, received notification each. Put calibrated settings
and lasting operating instructions in the service directory, then remove this plan.
