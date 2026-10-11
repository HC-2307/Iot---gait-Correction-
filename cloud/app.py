"""Flask app: the cloud side of the Level 6 system in one process.

Starts the MQTT ingest/analysis/controller service (service.py) in the background and serves
the dashboard and a JSON API.  Run: python app.py  then open http://localhost:5000
"""
import csv
import io
import json
import time

from flask import Flask, Response, jsonify, render_template, request

import service
from mqtt_client import topic

app = Flask(__name__)
db = service.db
mqtt = None


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
    })


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
