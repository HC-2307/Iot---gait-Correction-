// Copy this file to secrets.h and fill in your values. Don't commit secrets.h.
#pragma once

#define WIFI_SSID     "your-wifi"
#define WIFI_PASS     "your-wifi-password"

// Any MQTT broker works. Defaults are for flespi (free plan):
//   flespi.io -> sign up -> Tokens -> copy a token. The token is the username; the password stays empty.
#define MQTT_HOST     "mqtt.flespi.io"
#define MQTT_PORT     8883          // 8883 = TLS, 1883 = plain
#define MQTT_USE_TLS  1             // 0 for a plain broker on 1883 (e.g. Mosquitto on your laptop)
#define MQTT_USER     "your-flespi-token"
#define MQTT_PASS     ""            // empty for flespi

#define DEVICE_ID     "insole-01"   // unique per node
