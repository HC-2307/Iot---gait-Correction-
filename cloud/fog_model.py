"""Freeze-of-gait model: window features and live prediction.

train_fog.py trains the model on the FoG-STAR ankle IMU data (60 Hz, +-3 g) and saves it as
fog_model.joblib. The features use only the magnitudes of acceleration and angular velocity,
so they don't depend on how the sensor is turned. Without the file, predict() returns None and
the Freeze Index rule in analysis.py is the only freeze detector.
"""
from pathlib import Path

import numpy as np

FS = 60          # Hz: FoG-STAR's rate; live data is resampled to it
ACC_CLIP = 3.0   # g: FoG-STAR's accelerometer range
EPS = 1e-6
MODEL_PATH = Path(__file__).with_name("fog_model.joblib")
BANDS = [(0.5, 1.5), (1.5, 3), (3, 5), (5, 8), (8, 15)]   # Hz
STATS = (["mean", "std", "range", "jerk", "loco", "freeze", "fi", "domf", "entropy"]
         + [f"p{lo}-{hi}" for lo, hi in BANDS] + ["regularity", "period", "crossings"])

_bundle = None
_loaded_mtime = None


def feature_names(win_s, ctx_s):
    scales = ["win", "ctx"] if ctx_s > win_s else ["win"]
    return [f"{sig}_{sc}_{st}" for sig in ("acc", "gyro") for sc in scales for st in STATS]


def _stats(x):
    d = x - x.mean()
    p = np.abs(np.fft.rfft(d * np.hanning(len(x)))) ** 2 / len(x) ** 2
    f = np.fft.rfftfreq(len(x), 1 / FS)
    band = (f >= 0.5) & (f < 8)
    loco = p[(f >= 0.5) & (f < 3)].sum() + EPS   # locomotion band
    frz = p[(f >= 3) & (f < 8)].sum() + EPS      # freeze band (leg trembling)
    q = (p[band] + EPS) / (p[band] + EPS).sum()
    ac = np.correlate(d, d, "full")[len(d) - 1:]  # autocorrelation: steady stepping repeats, a freeze doesn't
    lo = int(0.4 * FS)
    ac = ac[lo:int(min(2.0, len(d) / FS / 2) * FS)] / (ac[0] + EPS)
    return ([x.mean(), np.log(x.std() + EPS), np.log(np.ptp(x) + EPS), np.log(np.abs(np.diff(x)).mean() + EPS),
             np.log(loco), np.log(frz), np.log(frz / loco), f[band][p[band].argmax()],
             -(q * np.log(q)).sum() / np.log(len(q))]
            + [np.log(p[(f >= a) & (f < b)].sum() + EPS) for a, b in BANDS]
            + [ac.max(), (ac.argmax() + lo) / FS, np.mean(d[:-1] * d[1:] < 0)])


def features(acc, gyro, win_s, ctx_s):
    """One feature row. acc (g) and gyro (dps) are (n, 3) arrays at FS: the last win_s seconds are
    the window being classified. With ctx_s > win_s the whole array (up to ctx_s seconds) is also
    described, as the context the window is compared with."""
    w = int(win_s * FS)
    out = []
    for sig in (np.linalg.norm(acc, axis=1), np.linalg.norm(gyro, axis=1)):
        out += _stats(sig[-w:])
        if ctx_s > win_s:
            out += _stats(sig)
    return out


def _load():
    global _bundle, _loaded_mtime
    if not MODEL_PATH.exists():
        _bundle = None
        return None
    mtime = MODEL_PATH.stat().st_mtime
    if mtime != _loaded_mtime:  # reload when the file is replaced
        import joblib
        _bundle = joblib.load(MODEL_PATH)
        _loaded_mtime = mtime
        print(f"[fog] loaded {MODEL_PATH.name}: {_bundle['name']}, threshold {_bundle['threshold']:.2f}")
    return _bundle


def threshold():
    b = _load()
    return b["threshold"] if b else None


def predict(a):
    """Freeze probability for a node buffer (rows of db.COLS at the node's own rate), or None."""
    b = _load()
    if b is None:
        return None
    t = a[:, 0] / 1000.0
    back = np.flatnonzero(np.diff(t) < 0)
    if len(back):  # the node restarted inside the buffer: use what came after
        a, t = a[back[-1] + 1:], t[back[-1] + 1:]
    if len(t) < 2 or t[-1] - t[0] < b["win_s"]:
        return None
    grid = t[-1] - np.arange(int(b["ctx_s"] * FS) - 1, -1, -1) / FS
    grid = grid[grid >= t[0]]
    x = np.column_stack([np.interp(grid, t, a[:, i]) for i in range(1, 7)])
    acc = np.clip(x[:, :3], -ACC_CLIP, ACC_CLIP)
    return float(b["model"].predict_proba([features(acc, x[:, 3:], b["win_s"], b["ctx_s"])])[0, 1])
