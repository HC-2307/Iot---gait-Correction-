// TEST - sensor ESP: publishes FSR press state to flespi; the actuator ESP reacts to it.
// Topic: test/<id>/fsr  {"f1":..,"f2":..,"p1":0|1,"p2":0|1}  retained, sent only when a press changes.
// Status: test/<id>/sensor/status  online/offline (offline = last will).
// Library: "PubSubClient" by Nick O'Leary. Board: ESP32 core 3.3.x. Serial 115200.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>

// ---- Fill these in ----
#define WIFI_SSID  "Harshit"            // 2.4 GHz, WPA2
#define WIFI_PASS  "harshit789"
#define MQTT_USER  ""    // flespi token is the username, password stays empty
#define DEVICE_ID  "insole-01"            // must match the actuator sketch and the dashboard

#define MQTT_HOST  "mqtt.flespi.io"
#define MQTT_PORT  8883

// ---- Pins ----
const int PIN_FSR1 = 32;  // heel, ADC1; 3.3 V - FSR - junction - 10k - GND
const int PIN_FSR2 = 34;  // forefoot, ADC1

const int FSR_THRESHOLD = 600;  // ADC counts = "pressed"; tune from the serial print
const int FSR_HYST      = 100;  // must drop below THRESHOLD - HYST to count as released (stops flicker spamming flespi)

WiFiClientSecure net;
PubSubClient mqtt(net);
String topicFsr, topicStatus;
int p1 = 0, p2 = 0;            // current press state
int sentP1 = -1, sentP2 = -1;  // last state flespi has; -1 forces a send

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
  String clientId = String(DEVICE_ID) + "-sensor-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(clientId.c_str(), MQTT_USER, nullptr, topicStatus.c_str(), 1, true, "offline")) {
    Serial.println("OK");
    mqtt.publish(topicStatus.c_str(), "online", true);
    sentP1 = sentP2 = -1;  // resend current state after a reconnect
  } else {
    char err[100]; net.lastError(err, sizeof(err));
    Serial.printf("failed, state=%d, tls: %s\n", mqtt.state(), err);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  analogSetAttenuation(ADC_11db);

  topicFsr    = String("test/") + DEVICE_ID + "/fsr";
  topicStatus = String("test/") + DEVICE_ID + "/sensor/status";

  net.setInsecure();  // test shortcut: TLS without certificate check
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(15);
  connectWiFi();
  Serial.printf("# sensor test, publishing to %s\n", topicFsr.c_str());
}

void loop() {
  connectWiFi();
  connectMqtt();
  mqtt.loop();

  static uint32_t nextRead = 0, nextPrint = 0;
  uint32_t now = millis();
  if ((int32_t)(now - nextRead) < 0) return;
  nextRead = now + 50;  // 20 Hz is plenty for a press test

  int f1 = analogRead(PIN_FSR1), f2 = analogRead(PIN_FSR2);
  p1 = f1 > (p1 ? FSR_THRESHOLD - FSR_HYST : FSR_THRESHOLD);
  p2 = f2 > (p2 ? FSR_THRESHOLD - FSR_HYST : FSR_THRESHOLD);

  if ((int32_t)(now - nextPrint) >= 0) {
    Serial.printf("f1=%4d f2=%4d  p1=%d p2=%d  mqtt=%s\n", f1, f2, p1, p2, mqtt.connected() ? "ok" : "down");
    nextPrint = now + 500;
  }

  if ((p1 != sentP1 || p2 != sentP2) && mqtt.connected()) {
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"f1\":%d,\"f2\":%d,\"p1\":%d,\"p2\":%d}", f1, f2, p1, p2);
    if (mqtt.publish(topicFsr.c_str(), buf, true)) {
      sentP1 = p1; sentP2 = p2;
      Serial.printf("# sent %s\n", buf);
    }
  }
}
