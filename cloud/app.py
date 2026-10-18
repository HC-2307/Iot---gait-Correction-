"""Flask app: the cloud side of the Level 6 system in one process.

Starts the MQTT ingest/analysis/controller service (service.py) in the background and serves
the dashboard and a JSON API.  Run: python app.py  then open http://localhost:5000
"""
import csv
import io
import json
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

import service
import sim_node
from mqtt_client import topic

app = Flask(__name__)
db = service.db
mqtt = None

SIM_ID, SIM_ACTUATOR, SIM_CYCLE_S = "insole-sim", "insole-01", 15
sim = {"thread": None, "stop": None, "state": {}}


def sim_running():
    return sim["thread"] is not None and sim["thread"].is_alive()


def rows(sql, params=()):
    cur = db.conn.cursor()
    with db.lock:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/nodes")
def nodes():
    now = time.time()
    out = rows("SELECT device, status, last_seen, msgs FROM nodes ORDER BY device")
    for n in out:
        n["live"] = n["status"] == "online" and now - (n["last_seen"] or 0) < 3
    return jsonify(out)


@app.get("/api/live")
def live():
    device = request.args["device"]
    seconds = float(request.args.get("seconds", 10))
    since = time.time() - seconds
    metrics = rows("SELECT * FROM metrics WHERE device=? ORDER BY ts DESC LIMIT 1", (device,))
    # Every 2nd sample (50 Hz) is plenty for the charts and halves the payload.
    samples = rows("SELECT ts, ax, ay, az, gx, gy, gz, tof, p1, p2, f1, f2 FROM samples "
                   "WHERE device=? AND ts > ? AND (rowid % 2) = 0 ORDER BY ts", (device, since))
    events = rows("SELECT ts, type, detail, auto_cued FROM events WHERE device=? ORDER BY ts DESC LIMIT 15",
                  (device,))
    session = db.open_session(device)
    return jsonify({
        "now": time.time(),
        "metrics": metrics[0] if metrics else None,
        "samples": {k: [s[k] for s in samples] for k in (samples[0].keys() if samples else [])},
        "events": events,
        "session": {"id": session[0], "label": session[1]} if session else None,
        "auto_cue": db.get_setting("auto_cue", "1") == "1",
        "sim": {"running": sim_running(), "mode": sim["state"].get("mode"), "device": SIM_ID},
        "flat": json.loads(db.get_setting(f"flat:{device}", "null")),
    })


@app.get("/api/latest")
def latest():
    """Full-rate samples newer than `after`, for the 3D foot."""
    return jsonify(rows("SELECT ts, ax, ay, az, gx, gy, gz, tof, p1, p2, f1, f2 FROM samples "
                        "WHERE device=? AND ts > ? ORDER BY ts LIMIT 100",
                        (request.args["device"], float(request.args.get("after") or time.time() - 0.5))))


@app.post("/api/sim")
def sim_toggle():
    """Start or stop the simulated insole. Its cues are forwarded to the real actuator node."""
    on = bool(request.get_json().get("on"))
    if on and not sim_running():
        sim["stop"], sim["state"] = threading.Event(), {}
        sim["thread"] = threading.Thread(target=sim_node.run, daemon=True, kwargs=dict(
            device=SIM_ID, cycle=SIM_CYCLE_S, cue_to=SIM_ACTUATOR, stop=sim["stop"], state=sim["state"]))
        sim["thread"].start()
    elif not on and sim_running():
        sim["stop"].set()
    return jsonify(ok=True, running=on)


@app.post("/api/cmd")
def cmd():
    body = request.get_json()
    device, payload = body["device"], body["payload"]
    text = json.dumps(payload)
    mqtt.publish(topic(device, "cmd"), text, qos=1)
    db.log_command(device, text, "dashboard")
    return jsonify(ok=True)


@app.post("/api/session")
def session():
    body = request.get_json()
    if body.get("action") == "start":
        sid = db.start_session(body["device"], body.get("label", "unlabelled"))
        return jsonify(ok=True, id=sid)
    db.stop_session(body["device"])
    return jsonify(ok=True)


@app.post("/api/settings")
def settings():
    body = request.get_json()
    if "auto_cue" in body:
        db.set_setting("auto_cue", "1" if body["auto_cue"] else "0")
    if "flat" in body:  # IMU and ToF baseline from the dashboard's "Set flat"
        f = body["flat"]
        db.set_setting(f"flat:{f['device']}", json.dumps(
            {"acc": [float(v) for v in f["acc"]], "gyro": [float(v) for v in f["gyro"]], "tof": float(f["tof"])}))
    return jsonify(ok=True)


@app.get("/api/sessions")
def sessions():
    return jsonify(rows("SELECT s.id, s.device, s.label, s.start_ts, s.end_ts, "
                        "(SELECT COUNT(*) FROM samples WHERE session=s.id) AS n FROM sessions s ORDER BY id DESC"))


@app.get("/api/export/<int:session_id>.csv")
def export(session_id):
    """Raw samples of one labelled recording, for training the model."""
    data = rows("SELECT * FROM samples WHERE session=? ORDER BY ts", (session_id,))
    label = (db.query("SELECT label FROM sessions WHERE id=?", (session_id,)) or [[""]])[0][0]
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0].keys()) + ["label"])
        w.writeheader()
        for r in data:
            w.writerow({**r, "label": label})
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=session_{session_id}_{label}.csv"})


if __name__ == "__main__":
    mqtt = service.start()
    # use_reloader=False so the MQTT service isn't started twice.
    app.run(host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
