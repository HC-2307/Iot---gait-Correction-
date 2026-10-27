"""Cloud service: MQTT ingest, storage, live analysis, ML model, and the central controller.

Started by the Flask app (app.py). Can also run alone: python service.py
Subscribes to every node (insole/+/data, insole/+/status), stores samples in SQLite,
recomputes gait metrics per node every second, logs abnormality events, and sends
biofeedback cue commands back to the node that produced them.
"""
import json
import threading
import time
from collections import defaultdict, deque

import numpy as np

import analysis
import config
import model
from db import DB
from mqtt_client import make_client, topic

db = DB()
buffers = defaultdict(lambda: deque(maxlen=config.WINDOW_S * config.FS))
buf_lock = threading.Lock()
last_event = defaultdict(float)   # (device, type) -> ts
last_cue = defaultdict(float)     # device -> ts
session_cache = {}                # device -> (checked_at, session_id)


def current_session(device):
    now = time.time()
    hit = session_cache.get(device)
    if hit and now - hit[0] < 1.0:
        return hit[1]
    s = db.open_session(device)
    session_cache[device] = (now, s[0] if s else None)
    return session_cache[device][1]


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[mqtt] connected: {reason_code}")
    client.subscribe([("insole/+/data", 0), ("insole/+/status", 1)])


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) != 3:
        return
    device, kind = parts[1], parts[2]
    if kind == "status":
        status = msg.payload.decode(errors="replace")
        db.touch_node(device, status=status)
        print(f"[node] {device}: {status}")
        return
    if kind != "data":
        return
    try:
        rows = json.loads(msg.payload)["s"]
    except (ValueError, KeyError):
        print(f"[warn] bad payload from {device}")
        return
    now = time.time()
    last_t = rows[-1][0]
    stamped = [(now - (last_t - r[0]) / 1000.0, r) for r in rows]  # wall-clock time per sample
    db.insert_samples(device, stamped, current_session(device))
    db.touch_node(device, status="online", msgs=1)
    with buf_lock:
        buffers[device].extend(rows)


def send_cmd(client, device, payload, source):
    body = json.dumps(payload)
    client.publish(topic(device, "cmd"), body, qos=1)
    db.log_command(device, body, source)


def analysis_loop(client):
    while True:
        time.sleep(config.ANALYSIS_EVERY_S)
        with buf_lock:
            snap = {d: np.array(b, dtype=float) for d, b in buffers.items() if len(b)}
        auto_cue = db.get_setting("auto_cue", "1") == "1"
        now = time.time()
        for device, a in snap.items():
            # Skip nodes that stopped sending (avoid re-reporting stale windows).
            if now - (db.query("SELECT last_seen FROM nodes WHERE device=?", (device,)) or [[0]])[0][0] > 3:
                continue
            m, events = analysis.analyze(a)
            ml_label, ml_conf, ml_probs = model.predict(m)
            db.execute(
                "INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device, now, m["walking"], m["strides"], m["cadence"], m["stride_time"], m["stride_cv"],
                 m["stance_pct"], m["heel_fore_ratio"], m["heel_impact"], m["swing_gyro"],
                 m["clearance_mm"], m["freeze_index"], m["turn_deg"], ml_label, ml_conf, m["freeze_prob"],
                 json.dumps(ml_probs) if ml_probs else None),
            )
            for etype, detail in events:
                if now - last_event[(device, etype)] < 3:  # debounce repeats
                    continue
                last_event[(device, etype)] = now
                cued = 0
                if auto_cue and etype in analysis.CUE_EVENTS and now - last_cue[device] > config.AUTO_CUE_COOLDOWN_S:
                    send_cmd(client, device, {"cmd": "cue", **analysis.CUE_EVENTS[etype]}, f"auto:{etype}")
                    last_cue[device] = now
                    cued = 1
                db.execute("INSERT INTO events(device, ts, type, detail, auto_cued) VALUES (?,?,?,?,?)",
                           (device, now, etype, detail, cued))
                print(f"[event] {device} {etype} ({detail}){' -> cue sent' if cued else ''}")


def start():
    """Connect to the broker and start ingest + analysis in background threads. Returns the MQTT client."""
    client = make_client("insole-backend")
    client.on_connect = on_connect
    client.on_message = on_message
    print(f"[mqtt] connecting to {config.MQTT_HOST}:{config.MQTT_PORT} (tls={config.MQTT_TLS})")
    client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()
    print(f"[db] {config.DB_PATH}")
    threading.Thread(target=analysis_loop, args=(client,), daemon=True).start()
    return client


if __name__ == "__main__":
    start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
