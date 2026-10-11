"""Simulated insole node: publishes synthetic gait in the same format as the ESP32.

Use it to test the backend and dashboard without hardware, and as a second (virtual)
node in the Level 6 demo. It is synthetic data, so present it as a simulated node.

  python sim_node.py                          # normal walking as insole-sim
  python sim_node.py --mode shuffle
  python sim_node.py --cycle 20               # normal -> shuffle -> freeze -> stand, 20 s each
  python sim_node.py --id insole-sim2 --mode freeze
"""
import argparse
import itertools
import json
import math
import random
import time

import config
from mqtt_client import make_client, topic

MODES = {
    #          stride s, swing dps, clearance mm, heel scale, piezo heel
    "normal":  (1.10, 320, 60, 1.0, 2200),
    "shuffle": (0.75, 120, 12, 0.35, 600),
}


def sample(mode, t):
    n = lambda s: random.gauss(0, s)
    base_tof = 45
    if mode in MODES:
        T, swing, clear, heel, piezo = MODES[mode]
        ph = (t % T) / T
        stance = ph < 0.6
        heel_f = 3000 * heel * math.sin(math.pi * ph / 0.35) if ph < 0.35 else 0
        fore_f = 2600 * math.sin(math.pi * (ph - 0.2) / 0.4) if 0.2 <= ph < 0.6 else 0
        sw = math.sin(math.pi * (ph - 0.6) / 0.4) if not stance else 0
        impact = 1.5 * heel * math.exp(-((ph - 0.01) / 0.015) ** 2)
        ax, ay, az = 0.4 * sw + n(0.03), n(0.03), 1 + impact - 0.3 * sw + n(0.03)
        gx, gy, gz = n(5), swing * sw + n(8), 8 * sw + n(5)
        tof = base_tof + clear * sw + n(2)
        p1 = int(piezo * math.exp(-((ph - 0.01) / 0.02) ** 2) + abs(n(20)))
        p2 = int(1200 * math.exp(-((ph - 0.5) / 0.03) ** 2) + abs(n(20)))
        f1, f2 = int(max(0, heel_f + n(30))), int(max(0, fore_f + n(30)))
    elif mode == "freeze":  # trembling in place: 5-6 Hz oscillation, no real steps
        tr = math.sin(2 * math.pi * 5.5 * t)
        ax, ay, az = 0.25 * tr + n(0.03), n(0.03), 1 + 0.15 * tr + n(0.03)
        gx, gy, gz = n(5), 35 * tr + n(5), n(5)
        tof = base_tof + 3 * abs(tr) + n(1)
        p1, p2 = int(abs(n(30))), int(abs(n(30)))
        f1, f2 = int(1500 + 300 * tr + n(30)), int(1500 - 300 * tr + n(30))
    else:  # stand
        ax, ay, az, gx, gy, gz = n(0.01), n(0.01), 1 + n(0.01), n(2), n(2), n(2)
        tof, p1, p2 = base_tof + n(1), int(abs(n(10))), int(abs(n(10)))
        f1, f2 = int(1400 + n(30)), int(1400 + n(30))
    return [round(ax, 3), round(ay, 3), round(az, 3), round(gx, 1), round(gy, 1), round(gz, 1),
            int(max(0, tof)), p1, p2, f1, f2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="insole-sim")
    ap.add_argument("--mode", default="normal", choices=["normal", "shuffle", "freeze", "stand"])
    ap.add_argument("--cycle", type=float, default=0, help="seconds per mode when cycling all modes")
    ap.add_argument("--no-fsr", action="store_true", help="send -1 for FSRs, like the real node today")
    args = ap.parse_args()

    client = make_client(args.id)
    client.will_set(topic(args.id, "status"), "offline", qos=1, retain=True)
    client.on_connect = lambda c, *_: (c.subscribe(topic(args.id, "cmd"), 1),
                                       c.publish(topic(args.id, "status"), "online", qos=1, retain=True))
    client.on_message = lambda c, u, m: print(f"[cmd] {m.payload.decode()}")
    client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()

    modes = itertools.cycle(["normal", "shuffle", "freeze", "stand"]) if args.cycle else None
    mode, switch_at = (next(modes) if modes else args.mode), time.time() + args.cycle
    print(f"[sim] {args.id} publishing to {config.MQTT_HOST}, mode={mode}")
    t0 = time.time()
    seq = 0
    next_send = time.time()
    try:
        while True:
            if modes and time.time() > switch_at:
                mode, switch_at = next(modes), time.time() + args.cycle
                print(f"[sim] mode={mode}")
            batch = []
            for k in range(10):
                tt = (next_send - t0) + k / config.FS
                row = [int(tt * 1000)] + sample(mode, tt)
                if args.no_fsr:
                    row[10] = row[11] = -1
                batch.append(row)
            client.publish(topic(args.id, "data"), json.dumps({"id": args.id, "seq": seq, "s": batch}))
            seq += 1
            next_send += 0.1
            time.sleep(max(0, next_send - time.time()))
    except KeyboardInterrupt:
        client.publish(topic(args.id, "status"), "offline", qos=1, retain=True)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
