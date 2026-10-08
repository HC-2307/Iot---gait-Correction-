// Smart insole - Level 6 IoT end node (ESP32, Arduino core)
// Samples all sensors at 100 Hz, batches 10 samples per MQTT message, listens for cloud commands.
// Topics: insole/<id>/data, insole/<id>/status (online/offline), insole/<id>/cmd
// Libraries: "Adafruit_VL53L0X" by Adafruit, "PubSubClient" by Nick O'Leary.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_VL53L0X.h>
// Copy this file to secrets.h and fill in your values. Don't commit secrets.h.
#pragma once

#define WIFI_SSID     "Arav's S25"
#define WIFI_PASS     "arav12345"

// Any MQTT broker works. Defaults are for flespi (free plan):
//   flespi.io -> sign up -> Tokens -> copy a token. The token is the username; the password stays empty.
#define MQTT_HOST     "mqtt.flespi.io"
#define MQTT_PORT     8883          // 8883 = TLS, 1883 = plain
#define MQTT_USE_TLS  1             // 0 for a plain broker on 1883 (e.g. Mosquitto on your laptop)
#define MQTT_USER     "3O1YJoscNTlCLTxJWBGMEh1M5Md8913fvyiceMwWQYLn7VJSf64zbsvAiq1Yi1gC"
#define MQTT_PASS     ""            // empty for flespi

#define DEVICE_ID     "insole-01"   // unique per node


// ---- Pins ----
const int PIN_SDA    = 25;   // MPU-6050 + VL53L0X share this I2C bus
const int PIN_SCL    = 26;
const int PIN_PIEZO1 = 35;
const int PIN_PIEZO2 = 33;
const int PIN_FSR1   = 34;   // heel
const int PIN_FSR2   = 32;   // forefoot
const int PIN_CUE    = 2;    // onboard LED; later the vibration motor driver
const bool USE_FSR   = false; // set true once the FSR resistors are in

const uint8_t  MPU_ADDR         = 0x68;
const uint32_t SAMPLE_PERIOD_US = 10000;  // 100 Hz
const int      BATCH            = 10;     // samples per MQTT message

String topicData, topicStatus, topicCmd;

#ifndef MQTT_USE_TLS
#define MQTT_USE_TLS 1
#endif
#if MQTT_USE_TLS
WiFiClientSecure net;
#else
WiFiClient net;
#endif
PubSubClient mqtt(net);
Adafruit_VL53L0X tof;
bool mpuOk = false, tofOk = false, streaming = true;
uint16_t lastTof = 0, piezo1Peak = 0, piezo2Peak = 0;
uint32_t seq = 0;

struct Sample { uint32_t t; float ax, ay, az, gx, gy, gz; uint16_t tof, p1, p2; int16_t f1, f2; };
QueueHandle_t sampleQueue;

volatile int cueRemaining = 0;
volatile uint32_t cueMs = 150;
uint32_t cueNext = 0;
bool cueOn = false, ledHold = false;

// ---------------- Sensors ----------------
void mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR); Wire.write(reg); Wire.write(val); Wire.endTransmission();
}

bool mpuInit() {
  Wire.beginTransmission(MPU_ADDR);
  if (Wire.endTransmission() != 0) return false;
  mpuWrite(0x6B, 0x00);  // wake
  mpuWrite(0x1A, 0x03);  // DLPF ~44 Hz
  mpuWrite(0x1B, 0x10);  // gyro  +-1000 dps
  mpuWrite(0x1C, 0x10);  // accel +-8 g
  return true;
}

bool mpuRead(Sample &s) {
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)14) != 14) return false;
  int16_t r[7];
  for (int i = 0; i < 7; i++) r[i] = (Wire.read() << 8) | Wire.read();
  s.ax = r[0] / 4096.0f; s.ay = r[1] / 4096.0f; s.az = r[2] / 4096.0f;
  s.gx = r[4] / 32.8f;   s.gy = r[5] / 32.8f;   s.gz = r[6] / 32.8f;
  return true;
}

void samplingTask(void *) {
  uint32_t nextTick = micros();
  for (;;) {
    uint16_t p1 = analogRead(PIN_PIEZO1), p2 = analogRead(PIN_PIEZO2);
    if (p1 > piezo1Peak) piezo1Peak = p1;
    if (p2 > piezo2Peak) piezo2Peak = p2;
    if (tofOk && tof.isRangeComplete())
      lastTof = tof.readRange();

    // 1 ms yield so loop() (WiFi/MQTT) on the same core gets CPU time.
    if ((int32_t)(micros() - nextTick) < 0) { vTaskDelay(1); continue; }
    nextTick += SAMPLE_PERIOD_US;
    if ((int32_t)(micros() - nextTick) > (int32_t)(5 * SAMPLE_PERIOD_US)) nextTick = micros();

    Sample s = {};
    s.t = millis();
    if (mpuOk) mpuRead(s);
    s.tof = lastTof;
    s.p1 = piezo1Peak; s.p2 = piezo2Peak;
    s.f1 = USE_FSR ? analogRead(PIN_FSR1) : -1;
    s.f2 = USE_FSR ? analogRead(PIN_FSR2) : -1;
    piezo1Peak = piezo2Peak = 0;
    xQueueSend(sampleQueue, &s, 0);
  }
}

// ---------------- Commands ----------------
void startCue(int n, uint32_t ms) { cueMs = ms; cueRemaining = n * 2; cueNext = 0; }

void onMessage(char *topic, byte *payload, unsigned int len) {
  String msg; msg.reserve(len);
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  Serial.printf("# cmd: %s\n", msg.c_str());

  auto intField = [&](const char *key, int def) {
    int k = msg.indexOf(String("\"") + key + "\"");
    if (k < 0) return def;
    int c = msg.indexOf(':', k);
    return c < 0 ? def : (int)msg.substring(c + 1).toInt();
  };
  bool on = msg.indexOf("\"on\":true") >= 0 || msg.indexOf("\"on\": true") >= 0;

  if (msg.indexOf("\"cue\"") >= 0)         startCue(intField("n", 3), intField("ms", 150));
  else if (msg.indexOf("\"led\"") >= 0)    { ledHold = on; digitalWrite(PIN_CUE, on); }
  else if (msg.indexOf("\"stream\"") >= 0) streaming = on;
  else if (msg.indexOf("\"ping\"") >= 0)   mqtt.publish(topicStatus.c_str(), "online", true);
}

void updateCue() {
  if (cueRemaining <= 0) return;
  uint32_t now = millis();
  if (now < cueNext) return;
  cueOn = !cueOn;
  digitalWrite(PIN_CUE, cueOn || ledHold);
  cueRemaining--;
  cueNext = now + cueMs;
  if (cueRemaining == 0) { cueOn = false; digitalWrite(PIN_CUE, ledHold); }
}

// ---------------- Connectivity ----------------

void connectWiFi() {
  // Non-blocking: start one attempt, give it 15 s, then retry.
  static uint32_t lastBegin = 0;
  static bool trying = false;
  wl_status_t st = WiFi.status();
  if (st == WL_CONNECTED) {
    if (trying) { Serial.printf("# WiFi: connected, IP %s\n", WiFi.localIP().toString().c_str()); trying = false; }
    return;
  }
  if (trying && millis() - lastBegin < 15000) return;
  if (trying) {
    const char *why = st == WL_NO_SSID_AVAIL ? "network not found (5 GHz only? name typo?)"
                    : st == WL_CONNECT_FAILED ? "rejected (wrong password or WPA3-only?)"
                    : "no answer";
    Serial.printf("# WiFi: failed, status %d: %s. Retrying\n", st, why);
  }
  Serial.printf("# WiFi: connecting to %s\n", WIFI_SSID);
  WiFi.disconnect(true);
  delay(100);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastBegin = millis();
  trying = true;
}

void connectMqtt() {
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  static uint32_t lastTry = 0;
  if (millis() - lastTry < 2000) return;
  lastTry = millis();
  Serial.print("# MQTT: connecting... ");
  String clientId = String(DEVICE_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  const char *pass = strlen(MQTT_PASS) ? MQTT_PASS : nullptr;  // flespi: token as user, no password
  if (mqtt.connect(clientId.c_str(), MQTT_USER, pass, topicStatus.c_str(), 1, true, "offline")) {
    Serial.println("OK");
    mqtt.publish(topicStatus.c_str(), "online", true);
    mqtt.subscribe(topicCmd.c_str(), 1);
  } else {
    Serial.printf("failed, state=%d\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_CUE, OUTPUT);
  digitalWrite(PIN_CUE, LOW);
  analogSetAttenuation(ADC_11db);

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
   for (int i = 0; i < 5 && !mpuOk; i++) { mpuOk = mpuInit(); if (!mpuOk) delay(200); }
  for (int i = 0; i < 5 && !tofOk; i++) {
    tofOk = tof.begin(0x29, false, &Wire, Adafruit_VL53L0X::VL53L0X_SENSE_HIGH_SPEED);
    if (!tofOk) delay(200);
  }
  if (tofOk) tof.startRangeContinuous(20);
  Serial.printf("# MPU-6050: %s, VL53L0X: %s, FSR: %s\n",
                mpuOk ? "OK" : "NOT FOUND", tofOk ? "OK" : "NOT FOUND", USE_FSR ? "on" : "off");

  topicData   = String("insole/") + DEVICE_ID + "/data";
  topicStatus = String("insole/") + DEVICE_ID + "/status";
  topicCmd    = String("insole/") + DEVICE_ID + "/cmd";

#if MQTT_USE_TLS
  net.setInsecure();  // demo shortcut: TLS without certificate check
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(2048);
  mqtt.setKeepAlive(15);

  connectWiFi();

  sampleQueue = xQueueCreate(BATCH * 20, sizeof(Sample));  // ~2 s of buffer
  xTaskCreatePinnedToCore(samplingTask, "sampling", 4096, nullptr, 2, nullptr, 1);
}

void loop() {
  connectWiFi();
  connectMqtt();
  mqtt.loop();
  updateCue();

  if (uxQueueMessagesWaiting(sampleQueue) < BATCH) { delay(1); return; }

  static char buf[1800];
  int n = snprintf(buf, sizeof(buf), "{\"id\":\"%s\",\"seq\":%lu,\"s\":[", DEVICE_ID, (unsigned long)seq++);
  for (int i = 0; i < BATCH; i++) {
    Sample s;
    xQueueReceive(sampleQueue, &s, 0);
    n += snprintf(buf + n, sizeof(buf) - n, "%s[%lu,%.3f,%.3f,%.3f,%.1f,%.1f,%.1f,%u,%u,%u,%d,%d]",
                  i ? "," : "", (unsigned long)s.t, s.ax, s.ay, s.az, s.gx, s.gy, s.gz,
                  s.tof, s.p1, s.p2, s.f1, s.f2);
  }
  n += snprintf(buf + n, sizeof(buf) - n, "]}");

  if (streaming && mqtt.connected()) mqtt.publish(topicData.c_str(), buf);
}