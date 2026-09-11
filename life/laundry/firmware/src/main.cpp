// laundry sensor — ESP32 + MPU-6050 (GY-521), one or two of them.
//
// WHAT THIS DEVICE DECIDES: nothing. It measures how much the appliance is
// shaking and posts that number every few seconds. Every threshold and every
// timer lives in the cluster (life/laundry), because this board ends up stuck
// to the back of a washing machine and "re-tune the threshold" must not mean
// "peel it off and find a USB cable".
//
// THE MEASUREMENT
// Accelerometer magnitude |a| = sqrt(ax²+ay²+az²), sampled at 100 Hz, reduced
// over a window to its STANDARD DEVIATION in milli-g:
//
//     var = E[|a|²] − E[|a|]²        (streaming, one pass, two accumulators)
//
// Taking the deviation rather than the level is what subtracts gravity — and
// with it, all dependence on how the board is oriented. A sensor mounted flat
// on the lid and one mounted sideways and upside down on the back panel produce
// the same reading for the same shaking. That is deliberate: it makes the
// physical install forgiving, which matters more than a tidier constant.
//
// Idle appliance reads ~2-5 mg (the MPU-6050's own noise floor is ~3 mg RMS
// over this bandwidth). A running washer reads tens to hundreds.
//
// WHAT IT DELIBERATELY DOES NOT DO
// It never buffers a failed post and replays it later. The server treats
// ARRIVAL time as sample time — the ESP32 has no RTC — so a replayed window
// would be a lie about the present. A dropped post is simply dropped; the
// server holds the cycle open across the gap rather than concluding anything.

#include <Arduino.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <WiFi.h>
#include <math.h>

#include "config.h"

// ---------------------------------------------------------------- MPU-6050
static const uint8_t REG_SMPLRT_DIV   = 0x19;
static const uint8_t REG_CONFIG       = 0x1A;
static const uint8_t REG_ACCEL_CONFIG = 0x1C;
static const uint8_t REG_ACCEL_XOUT_H = 0x3B;
static const uint8_t REG_PWR_MGMT_1   = 0x6B;
static const uint8_t REG_WHO_AM_I     = 0x75;

// ±2 g full scale. The most sensitive range, and the right one: we are
// measuring vibration in the tens of milli-g, not crash forces.
static const float ACCEL_LSB_PER_G = 16384.0f;

struct Sensor {
  uint8_t     addr;
  const char* device;      // must match a machine id in the chart's values.yaml
  bool        present;
  // Window accumulators.
  uint32_t    n;
  double      sum;         // Σ|a|      (g)
  double      sumsq;       // Σ|a|²     (g²)
  float       lo, hi;      // window extremes, for the peak figure
  float       tempC;
};

static Sensor sensors[] = {
#ifdef SENSOR_1_DEVICE
  {SENSOR_1_ADDR, SENSOR_1_DEVICE, false, 0, 0, 0, 0, 0, 0},
#endif
#ifdef SENSOR_2_DEVICE
  {SENSOR_2_ADDR, SENSOR_2_DEVICE, false, 0, 0, 0, 0, 0, 0},
#endif
};
static const size_t SENSOR_COUNT = sizeof(sensors) / sizeof(sensors[0]);

static uint32_t nextSampleMs = 0;
static uint32_t nextPostMs   = 0;
static uint32_t seq          = 0;

static bool writeReg(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool readRegs(uint8_t addr, uint8_t reg, uint8_t* buf, size_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)addr, (int)len) != (int)len) return false;
  for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

static bool initSensor(Sensor& s) {
  uint8_t who = 0;
  if (!readRegs(s.addr, REG_WHO_AM_I, &who, 1)) {
    Serial.printf("[mpu] 0x%02X: no reply — check SDA/SCL/3V3/GND\n", s.addr);
    return false;
  }
  // Genuine MPU-6050s answer 0x68; the MPU-6052/9250 clones sold as GY-521
  // answer 0x70, 0x72 or 0x73 and are register-compatible for our purposes.
  Serial.printf("[mpu] 0x%02X: WHO_AM_I = 0x%02X\n", s.addr, who);

  // Out of sleep, clocked from the X gyro PLL (more stable than the internal
  // oscillator, and the gyro is powered anyway).
  if (!writeReg(s.addr, REG_PWR_MGMT_1, 0x01)) return false;
  delay(50);
  // DLPF 44 Hz. An appliance's vibration is well under that, and filtering on
  // the chip beats filtering in software we would have to write.
  if (!writeReg(s.addr, REG_CONFIG, 0x03)) return false;
  // 1 kHz / (1 + 9) = 100 Hz internal sample rate, matching our poll rate.
  if (!writeReg(s.addr, REG_SMPLRT_DIV, 0x09)) return false;
  if (!writeReg(s.addr, REG_ACCEL_CONFIG, 0x00)) return false;  // ±2 g
  return true;
}

static void resetWindow(Sensor& s) {
  s.n = 0; s.sum = 0; s.sumsq = 0;
  s.lo = 1e9f; s.hi = -1e9f;
}

static void sample(Sensor& s) {
  if (!s.present) return;
  uint8_t b[14];
  // One burst: accel(6) + temp(2) + gyro(6). Reading them separately would
  // straddle the sensor's internal update and mix two different instants.
  if (!readRegs(s.addr, REG_ACCEL_XOUT_H, b, sizeof(b))) return;

  int16_t ax = (int16_t)((b[0] << 8) | b[1]);
  int16_t ay = (int16_t)((b[2] << 8) | b[3]);
  int16_t az = (int16_t)((b[4] << 8) | b[5]);
  int16_t rawT = (int16_t)((b[6] << 8) | b[7]);
  s.tempC = rawT / 340.0f + 36.53f;           // datasheet transfer function

  float x = ax / ACCEL_LSB_PER_G;
  float y = ay / ACCEL_LSB_PER_G;
  float z = az / ACCEL_LSB_PER_G;
  float mag = sqrtf(x * x + y * y + z * z);

  s.n++;
  s.sum   += mag;
  s.sumsq += (double)mag * mag;
  if (mag < s.lo) s.lo = mag;
  if (mag > s.hi) s.hi = mag;
}

// ------------------------------------------------------------------- wifi
static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  // The ESP32 defaults to sleeping the radio between beacons, which turns a
  // 5-second POST into a multi-second wait often enough to matter. This board
  // is mains-powered; there is nothing to save.
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) {
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] %s  rssi %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    // No reboot on failure: a reboot loop next to a running washer would miss
    // the whole load. Keep sampling, keep retrying, let the server see the gap.
    Serial.println("[wifi] failed; will retry");
  }
}

static void postWindow(Sensor& s, float rmsMg, float peakMg, uint32_t windowMs) {
  if (WiFi.status() != WL_CONNECTED) return;

  char url[160];
  snprintf(url, sizeof(url), "http://%s:%d/api/v1/samples",
           LAUNDRY_HOST, LAUNDRY_PORT);

  char body[256];
  snprintf(body, sizeof(body),
           "{\"device\":\"%s\",\"rms_mg\":%.2f,\"peak_mg\":%.2f,"
           "\"window_ms\":%lu,\"seq\":%lu,\"rssi\":%d,\"temp_c\":%.1f}",
           s.device, rmsMg, peakMg, (unsigned long)windowMs,
           (unsigned long)seq, WiFi.RSSI(), s.tempC);

  HTTPClient http;
  http.setConnectTimeout(4000);
  http.setTimeout(6000);
  if (!http.begin(url)) {
    Serial.println("[http] begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", LAUNDRY_INGEST_TOKEN);

  int code = http.POST((uint8_t*)body, strlen(body));
  if (code == 200) {
    String reply = http.getString();
    // The server echoes the machine's state; the LED is the only feedback you
    // get once this is behind an appliance, so it earns its place.
    bool running = reply.indexOf("\"running\":true") >= 0;
    digitalWrite(LED_PIN, running ? HIGH : LOW);
    Serial.printf("[post] %s rms=%.1f mg peak=%.1f mg -> %s\n",
                  s.device, rmsMg, peakMg, running ? "RUNNING" : "idle");
  } else {
    Serial.printf("[post] %s rms=%.1f mg -> HTTP %d (%s)\n",
                  s.device, rmsMg, code, http.errorToString(code).c_str());
  }
  http.end();
}

// ------------------------------------------------------------------ setup
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("laundry sensor starting");

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  // 100 kHz, not 400. If the second sensor is on a metre of jumper wire to the
  // machine next door, the extra capacitance makes 400 kHz marginal, and a
  // flaky bus reads as a quiet appliance.
  Wire.setClock(100000);

  for (size_t i = 0; i < SENSOR_COUNT; i++) {
    sensors[i].present = initSensor(sensors[i]);
    resetWindow(sensors[i]);
    Serial.printf("[mpu] %s @ 0x%02X: %s\n", sensors[i].device,
                  sensors[i].addr, sensors[i].present ? "ok" : "ABSENT");
  }

  ensureWifi();
  nextSampleMs = millis();
  nextPostMs   = millis() + POST_INTERVAL_MS;
}

void loop() {
  uint32_t now = millis();

  if ((int32_t)(now - nextSampleMs) >= 0) {
    nextSampleMs += SAMPLE_INTERVAL_MS;
    // If we fell behind (a slow POST, a wifi reconnect), resync rather than
    // sprinting through a backlog of missed slots.
    if ((int32_t)(now - nextSampleMs) > 0) nextSampleMs = now + SAMPLE_INTERVAL_MS;
    for (size_t i = 0; i < SENSOR_COUNT; i++) sample(sensors[i]);
  }

  if ((int32_t)(now - nextPostMs) >= 0) {
    uint32_t windowMs = POST_INTERVAL_MS;
    nextPostMs = now + POST_INTERVAL_MS;
    seq++;
    ensureWifi();

    for (size_t i = 0; i < SENSOR_COUNT; i++) {
      Sensor& s = sensors[i];
      if (!s.present) {
        // Try again: an intermittent sensor is usually a jumper that worked
        // itself loose, and it may come back.
        s.present = initSensor(s);
        // Drop whatever partial window it left behind. Without this, readings
        // taken before it dropped out get averaged into the NEXT window and
        // posted as if they were current — a few real samples diluted by a
        // gap, which reads as a machine going quiet.
        resetWindow(s);
        continue;
      }
      if (s.n < 2) { resetWindow(s); continue; }

      double mean = s.sum / s.n;
      double var  = (s.sumsq / s.n) - (mean * mean);
      if (var < 0) var = 0;                       // floating-point floor
      float rmsMg  = (float)(sqrt(var) * 1000.0);
      // Half the peak-to-peak spread of the window. A single figure for "how
      // hard did it bang", which is what separates a spin cycle from agitation.
      float peakMg = (s.hi - s.lo) * 0.5f * 1000.0f;

      postWindow(s, rmsMg, peakMg, windowMs);
      resetWindow(s);
    }
  }

  delay(1);
}
