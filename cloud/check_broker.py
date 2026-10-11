"""Quick broker check: connects with the settings in .env, publishes a test message, waits for it.

  python check_broker.py
"""
import sys
import time

import config
from mqtt_client import make_client

got = []
client = make_client("insole-check")
client.on_connect = lambda c, u, f, rc, p: (print(f"connect: {rc}"), c.subscribe("insole/check", 1))
client.on_message = lambda c, u, m: got.append(m.payload.decode())
print(f"connecting to {config.MQTT_HOST}:{config.MQTT_PORT} tls={config.MQTT_TLS} user={config.MQTT_USER[:6]}...")
try:
    client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
except Exception as e:
    sys.exit(f"FAILED to reach the broker: {e}")
client.loop_start()
time.sleep(2)
client.publish("insole/check", "hello", qos=1)
for _ in range(50):
    if got:
        print("OK: broker works, message round trip succeeded")
        break
    time.sleep(0.1)
else:
    print("FAILED: no message came back. A 'Not authorized' or 'Bad user name or password' "
          "above means the token is wrong or lacks MQTT access.")
client.loop_stop()
