"""SQLite storage shared by the ingest service and the dashboard."""
import sqlite3
import threading
import time

import config

COLS = ["t_ms", "ax", "ay", "az", "gx", "gy", "gz", "tof", "p1", "p2", "f1", "f2"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    device TEXT, ts REAL, t_ms INTEGER,
    ax REAL, ay REAL, az REAL, gx REAL, gy REAL, gz REAL,
    tof INTEGER, p1 INTEGER, p2 INTEGER, f1 INTEGER, f2 INTEGER,
    session INTEGER
);
CREATE INDEX IF NOT EXISTS samples_dev_ts ON samples(device, ts);
CREATE TABLE IF NOT EXISTS nodes (
    device TEXT PRIMARY KEY, status TEXT, first_seen REAL, last_seen REAL, msgs INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS metrics (
    device TEXT, ts REAL, walking INTEGER, strides INTEGER,
    cadence REAL, stride_time REAL, stride_cv REAL, stance_pct REAL, heel_fore_ratio REAL,
    heel_impact REAL, swing_gyro REAL, clearance_mm REAL, freeze_index REAL, turn_deg REAL,
    ml_label TEXT, ml_conf REAL, freeze_prob REAL, ml_probs TEXT
);
CREATE INDEX IF NOT EXISTS metrics_dev_ts ON metrics(device, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, device TEXT, ts REAL, type TEXT, detail TEXT, auto_cued INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, device TEXT, label TEXT, start_ts REAL, end_ts REAL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY, device TEXT, ts REAL, payload TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: str = config.DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        if "freeze_prob" not in [r[1] for r in self.conn.execute("PRAGMA table_info(metrics)")]:
            self.conn.execute("ALTER TABLE metrics ADD COLUMN freeze_prob REAL")  # databases made before 2026-10-10
        if "ml_probs" not in [r[1] for r in self.conn.execute("PRAGMA table_info(metrics)")]:
            self.conn.execute("ALTER TABLE metrics ADD COLUMN ml_probs TEXT")  # JSON {label: score}, added 2026-10-27
        self.lock = threading.Lock()

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # ---- nodes ----
    def touch_node(self, device, status=None, msgs=0):
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO nodes(device, status, first_seen, last_seen, msgs) VALUES (?,?,?,?,?) "
                "ON CONFLICT(device) DO UPDATE SET last_seen=excluded.last_seen, msgs=nodes.msgs+excluded.msgs, "
                "status=COALESCE(?, nodes.status)",
                (device, status or "online", now, now, msgs, status),
            )
            self.conn.commit()

    # ---- samples ----
    def insert_samples(self, device, rows, session):
        with self.lock:
            self.conn.executemany(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(device, ts, *r, session) for ts, r in rows],
            )
            self.conn.commit()

    # ---- sessions (labelled recordings) ----
    def open_session(self, device):
        r = self.query("SELECT id, label FROM sessions WHERE device=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1", (device,))
        return r[0] if r else None

    def start_session(self, device, label):
        self.stop_session(device)
        return self.execute("INSERT INTO sessions(device, label, start_ts) VALUES (?,?,?)",
                            (device, label, time.time())).lastrowid

    def stop_session(self, device):
        self.execute("UPDATE sessions SET end_ts=? WHERE device=? AND end_ts IS NULL", (time.time(), device))

    # ---- settings ----
    def get_setting(self, key, default=None):
        r = self.query("SELECT value FROM settings WHERE key=?", (key,))
        return r[0][0] if r else default

    def set_setting(self, key, value):
        self.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    def log_command(self, device, payload, source):
        self.execute("INSERT INTO commands(device, ts, payload, source) VALUES (?,?,?,?)",
                     (device, time.time(), payload, source))
