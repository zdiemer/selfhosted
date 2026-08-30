// Copy to config.h (gitignored) and fill in. Same rule as every values.local.yaml
// in this repo: the tracked file is the shape, the real one never enters git.
//
//     cp include/config.example.h include/config.h
//
#pragma once

// ---------------------------------------------------------------- network
#define WIFI_SSID     "REPLACE_ME"
#define WIFI_PASSWORD "REPLACE_ME"

// The cluster, BY IP, and this is not a placeholder to be tidied into a
// hostname later. Every *.zachd.duckdns.org name resolves to a TAILSCALE
// address (100.x), the cluster advertises no subnet routes, and an ESP32
// cannot run tailscaled — so the DuckDNS name is unreachable from this device
// even though it works from every laptop in the house. What does work is any
// k3s node's LAN address, where klipper-lb publishes the ingest LoadBalancer:
//
//     zachd-ubuntu    192.168.4.26      zachd-ubuntu-3  192.168.4.25
//     zachd-ubuntu-1  192.168.4.28      zachd-ubuntu-4  192.168.4.27
//     zachd-ubuntu-2  192.168.4.37      zachd-ubuntu-5  192.168.4.21
//
// Any one of them reaches the service — klipper-lb binds the port on every
// node and forwards to wherever the pod actually is, so this does NOT pin the
// device to a node. Prefer a control-plane node (.26 / .25 / .21): they are the
// least likely to be drained.
#define LAUNDRY_HOST "192.168.4.26"
#define LAUNDRY_PORT 8420

// Must equal secrets.ingestToken in values.local.yaml
// (op://homelab/life-laundry). Plain HTTP carries it, which is the same
// judgement infra/sms-relay makes about the handset on the LAN: the hop never
// leaves the house, and the alternative is a TLS stack and a cert store on a
// microcontroller for a payload that says a washing machine is shaking.
#define LAUNDRY_INGEST_TOKEN "REPLACE_ME"

// ----------------------------------------------------------------- wiring
// ESP32-D default I²C pins. Any two GPIOs work if you change them here.
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
// Onboard LED on most ESP32 DevKit boards. Lit = the server says this machine
// is running; dark = idle; flickering = trying to join wifi.
#define LED_PIN 2

// ----------------------------------------------------------------- sensors
// One entry per MPU-6050 on the bus. The device string MUST match a machine
// `id` in the chart's values.yaml, or the server answers 404 and says so.
//
// Address is set by the AD0 pin: unconnected or grounded = 0x68 (the GY-521
// has a pull-down on it), tied to 3V3 = 0x69. That is how two sensors share
// one ESP32 — see the README's "One ESP32 or two?".
#define SENSOR_1_DEVICE "washer"
#define SENSOR_1_ADDR   0x68

// Uncomment when the second sensor exists. Leave commented and this firmware
// is a one-machine build; nothing else changes.
// #define SENSOR_2_DEVICE "dryer"
// #define SENSOR_2_ADDR   0x69

// ------------------------------------------------------------------ timing
// 100 Hz sampling, 5 s windows. The window length is also the resolution of
// every timer on the server, and it is what the device costs the wifi: one
// small POST per machine per window, forever.
#define SAMPLE_INTERVAL_MS 10
#define POST_INTERVAL_MS   5000
