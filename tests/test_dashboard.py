"""Test dashboard for the two-ESP flespi loop: shows if each ESP is online and sends manual cues.

Uses the flespi settings in cloud/.env.  Run: python test_dashboard.py  then open http://localhost:5001
"""
import json
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
import config  # noqa: E402
from mqtt_client import make_client  # noqa: E402

DEVICE = "insole-01"  # must match DEVICE_ID in both sketches
BASE = f"test/{DEVICE}"
OUTPUTS = ("led", "buzzer", "motor")

status = {"sensor": "offline", "actuator": "offline", "broker": "connecting"}


def on_connect(client, userdata, flags, reason_code, properties):
    status["broker"] = "ok" if not reason_code.is_failure else f"failed: {reason_code}"
    client.subscribe(f"{BASE}/+/status", qos=1)


def on_message(client, userdata, msg):
    status[msg.topic.split("/")[2]] = msg.payload.decode()  # test/<id>/<sensor|actuator>/status


mqtt = make_client("test-dash")
mqtt.on_connect = on_connect
mqtt.on_message = on_message
mqtt.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
mqtt.loop_start()

app = Flask(__name__)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insole Loop Test</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 420px; margin: 40px auto; padding: 0 16px;
         background: #f6f7f9; color: #1d1f23; }
  .row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #ddd; }
  .on { color: #1a7f37; font-weight: 600; } .off { color: #b42318; font-weight: 600; }
  button { width: 100%; padding: 14px; margin-top: 12px; font-size: 16px; border: 0; border-radius: 8px;
           background: #2f5fd0; color: #fff; cursor: pointer; }
  button:active { background: #1f449c; }
  #msg { margin-top: 12px; min-height: 1.2em; color: #555; }
</style></head><body>
<h2>Insole loop test ({{ device }})</h2>
<div class="row"><span>flespi</span><span id="broker">...</span></div>
<div class="row"><span>Sensor ESP</span><span id="sensor">...</span></div>
<div class="row"><span>Actuator ESP</span><span id="actuator">...</span></div>
{% for o in outputs %}<button onclick="cue('{{ o }}')">Cue {{ o }}</button>{% endfor %}
<div id="msg"></div>
<script>
async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    for (const k of ['broker', 'sensor', 'actuator']) {
      const el = document.getElementById(k);
      el.textContent = s[k];
      el.className = (s[k] === 'online' || s[k] === 'ok') ? 'on' : 'off';
    }
  } catch (e) { document.getElementById('broker').textContent = 'dashboard down'; }
}
async function cue(out) {
  const r = await fetch('/api/cue', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify({out})});
  document.getElementById('msg').textContent = r.ok ? 'Sent ' + out + ' cue' : 'Send failed';
}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


@app.get("/")
def index():
    return render_template_string(PAGE, device=DEVICE, outputs=OUTPUTS)


@app.get("/api/status")
def api_status():
    return jsonify(status)


@app.post("/api/cue")
def api_cue():
    out = (request.get_json(silent=True) or {}).get("out")
    if out not in OUTPUTS:
        return jsonify(error="out must be led, buzzer or motor"), 400
    mqtt.publish(f"{BASE}/cmd", json.dumps({"out": out, "n": 3, "ms": 150}), qos=1)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(port=5001, use_reloader=False)
