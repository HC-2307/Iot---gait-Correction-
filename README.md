# Smart Insole: IoT gait correction (Level 6 IoT)

A smart insole for gait correction in Parkinson's disease. One insole on one foot senses the gait, a
Flask app works out what is wrong, and a separate actuator node cues the wearer with vibration,
sound and light.

Gait problems in scope: freezing of gait and start hesitation, shuffling, festination, reduced gait
speed, and diminished heel strike or flat-footed contact.

**Status:** both firmware sketches compile for the ESP32 (core 3.3.x). The freeze, shuffling,
foot-drag and heel-strike checks are rule-based for now; the trained model is work in progress.

```
firmware/insole_node.ino    sensor ESP32: 100 Hz sampling, MQTT publish, cloud commands
firmware/actuator_node.ino  actuator ESP32: vibration motor, buzzer and LED driven by cloud commands
cloud/app.py                Flask app: starts the service below and serves the dashboard + JSON API
cloud/service.py            MQTT ingest + storage (SQLite) + live gait analysis + central controller
cloud/analysis.py           gait metrics and abnormality rules (thresholds in THRESH)
cloud/model.py              runs the trained classifier (model.joblib) on every analysis window
cloud/train_model.py        trains model.joblib from labelled recordings
cloud/templates/            dashboard page (Chart.js served from cloud/static, works offline)
cloud/sim_node.py           simulated node (same message format) for testing and the multi-node demo
tests/                      small sketches and a test dashboard for checking the sensor-to-actuator loop
```

## Data flow

```
Sensor ESP32 --WiFi/MQTT TLS--> MQTT broker <--> Flask app (cloud/app.py) --> dashboard in the browser
Actuator ESP32 <-- subscribes to insole/<id>/cmd ---'
```

The broker (flespi by default) only relays messages. Storage, analytics, the ML model and the
dashboard all live in the Flask app.

Back the other way: `service.py` (automatic cues on FREEZING / SHUFFLING / FOOT_DRAG) and the
dashboard buttons publish to `insole/<id>/cmd`. The actuator node pulses its motor, buzzer and LED
together, and the sensor node blinks its onboard LED.

Topics:

| Topic | Content |
|---|---|
| `insole/<id>/data` | 10 samples per message at 100 Hz: `[t_ms, ax, ay, az, gx, gy, gz, tof, p1, p2, f1, f2]` (g, dps, mm, raw ADC) |
| `insole/<id>/status` | sensor node `online` / `offline`, retained, last will |
| `insole/<id>-act/status` | actuator node `online` / `offline`, retained, last will |
| `insole/<id>/cmd` | JSON commands: `{"cmd":"cue","n":3,"ms":150}`, `{"cmd":"led","on":true}`, `{"cmd":"stream","on":false}`, `{"cmd":"ping"}` |

## 1. Broker

Any MQTT broker works. The settings live at the top of each firmware sketch and in `cloud/.env`
(host, port, TLS on/off, user, password). The defaults are for **flespi** (free plan):

1. Sign up at flespi.io, open **Tokens**, and copy a token.
2. Host `mqtt.flespi.io`, port `8883` (TLS). Username = the token, password = empty.
3. Both ESP32s and the Flask app can use the same token (they connect with different client IDs).
4. Put the token in `cloud/.env` and run `python check_broker.py`. It should print `OK`.

The free plan's message limit is far above what one insole sends (about 10 messages/s).
For a fully offline demo, run Mosquitto on the laptop instead: port 1883, `MQTT_USE_TLS 0` / `MQTT_TLS=0`.

## 2. Flask app (laptop or a VM)

```
cd cloud
python -m pip install -r requirements.txt
cp .env.example .env            # fill in the broker host, user, password
python app.py                   # open http://localhost:5000
python sim_node.py --cycle 20   # optional, second terminal: simulated node cycling normal/shuffle/freeze/stand
```

## 3. Model

1. In the dashboard, pick a label and press Start recording, walk that way for 30-60 s, press Stop.
   Record at least two sessions per label (normal, shuffling, freezing, ...).
2. `python train_model.py` cuts every recording into windows, computes the same features as the live
   analysis, trains a Random Forest, prints cross-validation by recording, and saves `model.joblib`.
3. The running app loads the new model automatically; the Model tile shows its live prediction.
   Session CSVs can be downloaded from the Recordings list (`/api/export/<id>.csv`), and
   `python train_model.py a.csv b.csv` trains from those files instead.

## 4. Firmware

Board: ESP32-WROOM-32 dev board ("ESP32 Dev Module"), one for the sensors and one for the actuators.

1. Arduino IDE: install the ESP32 board package (3.3.x), then the libraries "PubSubClient"
   (Nick O'Leary) and "Adafruit_VL53L0X" (Adafruit).
2. Open each sketch and fill in `WIFI_SSID`, `WIFI_PASS` and `MQTT_USER` (the flespi token) at the
   top. They are left empty in the repo: don't commit real values. Use the same values and the same
   `DEVICE_ID` in both sketches. The WiFi must be 2.4 GHz with WPA2.
3. Flash `insole_node.ino` to the sensor ESP32. Serial Monitor at 115200 should show
   `MPU-6050: OK, VL53L0X: OK, FSR: on`, the WiFi IP, then `MQTT: connecting... OK`.
4. Flash `actuator_node.ino` to the actuator ESP32. It pulses all three outputs once at boot, then
   prints `# listening on insole/insole-01/cmd`.
5. Both nodes appear in the dashboard's node list. Keep the insole (`insole-01`) selected; the
   actuator (`insole-01-act`) sends no data. Press "Send cue": the motor, buzzer and LED pulse 3 times.

Sensor ESP32 pins:

| Part | GPIO | Notes |
|---|---|---|
| MPU-6050 and VL53L0X, SDA / SCL | 25 / 26 | shared I2C bus (0x68 and 0x29) |
| Piezo, heel / forefoot | 33 / 35 | |
| FSR, heel / forefoot | 32 / 34 | 3.3 V - FSR - junction - 10 kΩ - GND |
| Cue LED | 2 | onboard |

Actuator ESP32 pins:

| Part | GPIO | Notes |
|---|---|---|
| Vibration motor | 32 | through a 2N7000 MOSFET |
| LED | 26 | 220 Ω resistor |
| Active buzzer | 27 | |

All analog inputs are on ADC1 (GPIO 32-39). ADC2 pins read nothing while WiFi is on.

## Tuning

Thresholds in `cloud/analysis.py` (`THRESH`) are starting values tested only on simulated data.
Record a labelled session for each pattern from the dashboard (Labelled recording), then adjust:
the `samples` table carries the session id and `sessions` holds the label.
`YAW_AXIS` must be the gyro axis that points up, which depends on how the MPU-6050 is mounted.

## Known shortcuts for the demo

- `net.setInsecure()` in the firmware skips TLS certificate checks. Pin the CA cert for the final build.
- SQLite on the machine running `service.py`. Move to a hosted DB if the reviewers want storage off the laptop.
- Cue commands are limited to 20 pulses of at most 2 s each, so a bad message cannot run the motor for long.
