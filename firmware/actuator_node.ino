// Smart insole - actuator ESP32 (Arduino core 3.3.x)
// Listens on the insole's command topic and pulses the vibration motor, buzzer and LED together.
// Topics: insole/<id>/cmd (subscribe), insole/<id>-act/status (online/offline, offline = last will)
// Commands: {"cmd":"cue","n":3,"ms":150}  {"cmd":"led","on":true}  {"cmd":"ping"}
// Library: "PubSubClient" by Nick O'Leary. Serial 115200.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>

// ---- Fill these in ----
#define WIFI_SSID     ""            // phone hotspot: 2.4 GHz, WPA2
#define WIFI_PASS     ""
#define MQTT_USER     ""            // flespi token (the token is the username)
#define MQTT_PASS     ""            // stays empty for flespi

#define MQTT_HOST     "mqtt.flespi.io"
#define MQTT_PORT     8883          // 8883 = TLS, 1883 = plain
#define MQTT_USE_TLS  1             // 0 for a plain broker on 1883 (e.g. Mosquitto on the laptop)
#define DEVICE_ID     "insole-01"   // the insole this actuator follows; must match the sensor sketch

// ---- Pins ----
const int PIN_MOTOR  = 32;   // vibration motor through a 2N7000 MOSFET
const int PIN_LED    = 26;   // LED with 220 ohm
const int PIN_BUZZER = 27;   // active buzzer

String topicCmd, topicStatus;

#if MQTT_USE_TLS
WiFiClientSecure net;
#else
WiFiClient net;
#endif
PubSubClient mqtt(net);

int cueRemaining = 0;        // on/off phases left
uint32_t cueMs = 150, cueNext = 0;
bool cueOn = false, ledHold = false;

// ---------------- Outputs ----------------
void setOutputs(bool on) {
  digitalWrite(PIN_MOTOR, on);
  digitalWrite(PIN_BUZZER, on);
  digitalWrite(PIN_LED, on || ledHold);
}

void startCue(int n, uint32_t ms) { cueMs = ms; cueRemaining = n * 2; cueNext = 0; }

void updateCue() {
  if (cueRemaining <= 0) return;
  uint32_t now = millis();
  if (now < cueNext) return;
  cueOn = !cueOn;
  setOutputs(cueOn);
  cueRemaining--;
  cueNext = now + cueMs;
  if (cueRemaining == 0) { cueOn = false; setOutputs(false); }
}

// ---------------- Commands ----------------
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

  // Limits so a bad message cannot run the motor for minutes.
  if (msg.indexOf("\"cue\"") >= 0)       startCue(constrain(intField("n", 3), 1, 20), constrain(intField("ms", 150), 20, 2000));
  else if (msg.indexOf("\"led\"") >= 0)  { ledHold = on; digitalWrite(PIN_LED, on || cueOn); }
  else if (msg.indexOf("\"ping\"") >= 0) mqtt.publish(topicStatus.c_str(), "online", true);
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
  String clientId = String(DEVICE_ID) + "-act-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  const char *pass = strlen(MQTT_PASS) ? MQTT_PASS : nullptr;  // flespi: token as user, no password
  if (mqtt.connect(clientId.c_str(), MQTT_USER, pass, topicStatus.c_str(), 1, true, "offline")) {
    Serial.println("OK");
    mqtt.publish(topicStatus.c_str(), "online", true);
    mqtt.subscribe(topicCmd.c_str(), 1);
    Serial.printf("# listening on %s\n", topicCmd.c_str());
  } else {
    Serial.printf("failed, state=%d\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  if (!strlen(WIFI_SSID) || !strlen(MQTT_USER))
    Serial.println("# Fill in WIFI_SSID, WIFI_PASS and MQTT_USER at the top of this sketch.");
  pinMode(PIN_MOTOR, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  setOutputs(false);

  topicCmd    = String("insole/") + DEVICE_ID + "/cmd";
  topicStatus = String("insole/") + DEVICE_ID + "-act/status";

#if MQTT_USE_TLS
  net.setInsecure();  // demo shortcut: TLS without certificate check
#endif
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setKeepAlive(15);

  startCue(1, 200);  // one pulse at boot shows all three outputs work
  connectWiFi();
}

void loop() {
  connectWiFi();
  connectMqtt();
  mqtt.loop();
  updateCue();
  delay(1);
}
