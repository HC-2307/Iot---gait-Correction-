# Smart Insole: Level 6 IoT code

```
firmware/insole_node/   ESP32 end node: 100 Hz sampling, MQTT publish, cloud commands
cloud/app.py            Flask app: starts the service below and serves the dashboard + JSON API
cloud/service.py        MQTT ingest + storage (SQLite) + live gait analysis + central controller
cloud/analysis.py       gait metrics and abnormality rules (thresholds in THRESH)
cloud/model.py          runs the trained classifier (model.joblib) on every analysis window
cloud/train_model.py    trains model.joblib from labelled recordings
cloud/templates/        dashboard page (Chart.js served from cloud/static, works offline)
cloud/sim_node.py       simulated node (same message format) for testing and the multi-node demo
insole_bringup/         serial-only sensor test sketch
```

## Data flow

ESP32 node → WiFi → MQTT broker (flespi by default) → Flask app (`app.py`: storage, analytics, model) → dashboard in the browser.

The broker only relays messages between the ESP32 and the Flask app. Storage, analytics, the ML model and the dashboard all live in the Flask app.
Back the other way: `service.py` (automatic cues on FREEZING / SHUFFLING / FOOT_DRAG) and the
dashboard buttons publish to `insole/<id>/cmd`, and the node blinks its cue LED (GPIO2 for now,
vibration motor later).

Topics: `insole/<id>/data` (10 samples per message), `insole/<id>/status` (online/offline, retained,
last will), `insole/<id>/cmd` (JSON commands, see the top of `insole_node.ino`).

## 1. Broker

Any MQTT broker works; the settings live only in `firmware/insole_node/secrets.h` and `cloud/.env`
(host, port, TLS on/off, user, password). The defaults are for **flespi** (free plan):

1. Sign up at flespi.io, open **Tokens**, and copy a token.
2. Host `mqtt.flespi.io`, port `8883` (TLS). Username = the token, password = empty.
3. The ESP32 and the Flask app can use the same token (they connect with different client IDs).
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

1. Arduino IDE: install the ESP32 board package, then libraries "PubSubClient" (Nick O'Leary) and "VL53L0X" (Pololu).
2. Copy `secrets_example.h` to `secrets.h` in the same folder and fill in WiFi + broker.
3. Flash `insole_node.ino`, open Serial Monitor at 115200: you should see sensor status, WiFi IP, `MQTT: OK`.
4. The node appears in the dashboard's node list within a second.
5. When the FSRs are wired (heel GPIO34, forefoot GPIO32, 10 kΩ to GND), set `USE_FSR = true` and reflash.

## Tuning

Thresholds in `cloud/analysis.py` (`THRESH`) are starting values tested only on simulated data.
Record a labelled session for each pattern from the dashboard (Labelled recording), then adjust:
the `samples` table carries the session id and `sessions` holds the label.
`YAW_AXIS` must be the gyro axis that points up, which depends on how the MPU-6050 is mounted.

## Known shortcuts for the demo

- `net.setInsecure()` in the firmware skips TLS certificate checks. Pin the CA cert for the final build.
- SQLite on the machine running `service.py`. Move to a hosted DB if the reviewers want storage off the laptop.
# Iot---gait-Correction-
