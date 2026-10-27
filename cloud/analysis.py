"""Gait metrics and abnormality detection on a sliding window of node samples.

Input: numpy array (N x 12) with columns db.COLS, sampled at config.FS.
All thresholds in THRESH are starting values. Tune them on your own labelled walks.
"""
import numpy as np

import config
import fog_model

FS = config.FS
I = {c: i for i, c in enumerate(["t_ms", "ax", "ay", "az", "gx", "gy", "gz", "tof", "p1", "p2", "f1", "f2"])}

THRESH = {
    "fsr_contact": 600,        # ADC counts: FSR above this = foot loaded
    "swing_gyro_min": 100,     # dps: gyro peak that counts as a swing (stride) when FSRs are off
    "min_stride_s": 0.5,
    "max_stride_s": 2.5,
    "active_acc_std": 0.05,    # g: below this the person is standing still
    "fi_freeze": 2.5,          # Freeze Index above this = freezing candidate
    "fi_power_min": 0.002,     # g^2 in 0.5-8 Hz: needed so a still foot isn't called a freeze
    "fog_prob": None,          # freeze-model probability that raises an alarm; None = the one train_fog.py chose
    "shuffle_swing_gyro": 170, # dps: weaker swing than this while walking = shuffling
    "shuffle_stride_s": 0.85,  # and short strides
    "drag_clearance_mm": 25,   # foot clearance below this while walking = dragging
    "heel_ratio_low": 0.6,     # heel/forefoot peak ratio below this = reduced heel strike
    "piezo_heel_low": 900,     # piezo-1 heel impact below this = reduced heel strike (no FSR)
    "stride_cv_high": 8.0,     # % stride-time variability
    "turn_deg": 60,            # yaw change in 2 s that counts as a turn
    "tof_valid_max": 1500,     # mm: readings above are out of range
}
YAW_AXIS = "gz"  # which gyro axis is vertical depends on how the MPU is mounted


def _peaks(x, height, min_dist):
    """Indices of local maxima above height, at least min_dist samples apart (keeps the larger)."""
    cand = np.flatnonzero((x[1:-1] > height) & (x[1:-1] >= x[:-2]) & (x[1:-1] > x[2:])) + 1
    keep = []
    for i in cand:
        if keep and i - keep[-1] < min_dist:
            if x[i] > x[keep[-1]]:
                keep[-1] = i
        else:
            keep.append(i)
    return np.array(keep, dtype=int)


def freeze_index(acc_mag):
    """Moore et al. Freeze Index: power in 3-8 Hz 'freeze band' over 0.5-3 Hz 'locomotion band'."""
    x = acc_mag - acc_mag.mean()
    x = x * np.hanning(len(x))
    p = np.abs(np.fft.rfft(x)) ** 2 / len(x)
    f = np.fft.rfftfreq(len(x), 1 / FS)
    loco = p[(f >= 0.5) & (f < 3)].sum()
    frz = p[(f >= 3) & (f < 8)].sum()
    return frz / (loco + 1e-9), 2 * (loco + frz) / len(x)  # second value ~ signal variance in 0.5-8 Hz


def pattern_features(a, gyr, steps):
    """Extra features for the gait-pattern model. Both piezos sit on the outer edge of the sole
    (p1 outer front of the heel, p2 outer midfoot), so piezo hits are measured above each sensor's own rest level."""
    g = a[:, [I["gx"], I["gy"], I["gz"]]]
    ac = a[:, [I["ax"], I["ay"], I["az"]]]
    d = {}
    hits = {}
    for n in ("p1", "p2"):
        p = a[:, I[n]]
        base = np.median(p)
        pk = _peaks(p - base, 300, FS // 3)
        hits[n] = pk
        d[f"{n}_peak"] = float(np.median(p[pk] - base)) if len(pk) else 0.0
        d[f"{n}_hits_per_step"] = len(pk) / max(len(steps), 1)
        d[f"{n}_iqr"] = float(np.percentile(p, 75) - np.percentile(p, 25))
        d[f"{n}_sat"] = float((p >= 4090).mean())
    # roll along the outer edge: time from each heel-side hit to the next midfoot hit
    k1, k2 = hits["p1"], hits["p2"]
    lags = [(k2[k2 > i][0] - i) / FS for i in k1 if (k2 > i).any() and k2[k2 > i][0] - i < FS]
    d["roll_lag_s"] = float(np.median(lags)) if lags else 0.0
    p1, p2 = a[:, I["p1"]], a[:, I["p2"]]
    d["p1_p2_corr"] = float(np.nan_to_num(np.corrcoef(p1, p2)[0, 1])) if p1.std() > 0 and p2.std() > 0 else 0.0
    for n in ("f1", "f2"):
        f = a[:, I[n]]
        d[f"{n}_range"] = float(np.percentile(f, 95) - np.percentile(f, 5))
    for i, ax in enumerate("xyz"):
        d[f"g{ax}_std"] = float(g[:, i].std())
        d[f"a{ax}_std"] = float(ac[:, i].std())
        d[f"a{ax}_p95"] = float(np.percentile(ac[:, i], 95) - np.median(ac[:, i]))
    acc = np.linalg.norm(ac, axis=1)
    d["acc_peak"] = float(np.median([acc[max(0, s - 30):s + 30].max() for s in steps])) if len(steps) else 0.0
    d["swing_peak"] = float(np.median(gyr[steps])) if len(steps) else 0.0
    return d


def analyze(a: np.ndarray):
    """Returns (metrics dict, list of (event_type, detail))."""
    m = {k: None for k in ["walking", "strides", "cadence", "stride_time", "stride_cv", "stance_pct",
                           "heel_fore_ratio", "heel_impact", "swing_gyro", "clearance_mm",
                           "freeze_index", "turn_deg", "freeze_prob", "acc_std", "gyro_rms", "rotation_deg"]}
    events = []
    if len(a) < 4 * FS:
        return m, events

    t = a[:, I["t_ms"]] / 1000.0
    acc = np.linalg.norm(a[:, [I["ax"], I["ay"], I["az"]]], axis=1)
    gyr = np.linalg.norm(a[:, [I["gx"], I["gy"], I["gz"]]], axis=1)
    f1, f2 = a[:, I["f1"]], a[:, I["f2"]]
    have_fsr = bool((f1 >= 0).all() and (f2 >= 0).all())
    last4 = slice(-4 * FS, None)

    # ---- strides ----
    # One gyro swing peak per stride. The heel FSR never unloads on the real insole (stays above ~2100),
    # so FSR edges can't mark steps; they only give the stance figure.
    contacts = _peaks(gyr, THRESH["swing_gyro_min"], int(THRESH["min_stride_s"] * FS))
    if have_fsr:
        loaded = (f1 > THRESH["fsr_contact"]) | (f2 > THRESH["fsr_contact"])
        m["stance_pct"] = float(loaded.mean() * 100)

    st = np.diff(t[contacts]) if len(contacts) > 1 else np.array([])
    st = st[(st >= THRESH["min_stride_s"]) & (st <= THRESH["max_stride_s"])]
    m["strides"] = int(len(st))
    recent_stride = len(contacts) > 0 and (t[-1] - t[contacts[-1]]) < 2.0
    acc_std = float(acc[last4].std())
    walking = len(st) >= 2 and recent_stride and acc_std > THRESH["active_acc_std"]
    m["walking"] = int(walking)

    # ---- overall movement (whole window), for the gait-pattern model ----
    m["acc_std"] = float(acc.std())                     # g: how much the foot moves
    m["gyro_rms"] = float(np.sqrt(np.mean(gyr ** 2)))  # dps: swing energy
    m["rotation_deg"] = float(np.abs(a[:, [I["gx"], I["gy"], I["gz"]]].sum(axis=0) / FS).max())  # largest net turn on any axis
    m.update(pattern_features(a, gyr, contacts))

    if len(st) and walking:
        m["stride_time"] = float(st.mean())
        m["cadence"] = float(120.0 / st.mean())  # steps/min, two steps per stride of one foot
        if len(st) >= 3:
            m["stride_cv"] = float(st.std() / st.mean() * 100)

    # ---- swing strength (shuffling) ----
    pk = _peaks(gyr, THRESH["swing_gyro_min"] * 0.5, int(THRESH["min_stride_s"] * FS))
    if len(pk) and walking:
        m["swing_gyro"] = float(np.median(gyr[pk]))

    # ---- heel strike ----
    piezo_pk = _peaks(a[:, I["p1"]], 50, int(THRESH["min_stride_s"] * FS))
    if len(piezo_pk):
        m["heel_impact"] = float(np.median(a[piezo_pk, I["p1"]]))
    if have_fsr and walking:
        m["heel_fore_ratio"] = float(np.percentile(f2, 95) / (np.percentile(f1, 95) + 1e-9))  # f2 = heel (pin 34), f1 = forefoot (pin 32)

    # ---- foot clearance (ToF) ----
    tof = a[last4, I["tof"]]  # recent window only, so standing earlier doesn't hide the swing
    ok = (tof > 0) & (tof < THRESH["tof_valid_max"])
    if ok.sum() > FS:
        m["clearance_mm"] = float(np.percentile(tof[ok], 90) - np.percentile(tof[ok], 10))

    # ---- freezing ----
    fi, power = freeze_index(acc[last4])
    m["freeze_index"] = float(fi)
    m["freeze_prob"] = fog_model.predict(a)  # None until train_fog.py has saved a model

    # ---- turning ----
    yaw = a[-2 * FS:, I[YAW_AXIS]]
    m["turn_deg"] = float(np.sum(yaw) / FS)

    # ---- events ----
    # The trained model goes first; the Freeze Index rule stays as the fallback and for plain trembling.
    # The model takes a foot at rest for a motionless freeze about half the time (FoG-STAR sit/stand
    # windows), so it only alarms if the window holds a step or the leg is trembling.
    fog_thr = THRESH["fog_prob"] or fog_model.threshold()
    still = not len(contacts) and power <= THRESH["fi_power_min"]
    if m["freeze_prob"] is not None and m["freeze_prob"] >= fog_thr and not still:
        events.append(("FREEZING", f"model p={m['freeze_prob']:.2f} (alarm at {fog_thr:.2f})"))
    elif fi > THRESH["fi_freeze"] and power > THRESH["fi_power_min"] and not recent_stride:
        events.append(("FREEZING", f"freeze index {fi:.1f}"))
    if walking:
        if m["swing_gyro"] is not None and m["swing_gyro"] < THRESH["shuffle_swing_gyro"] \
                and m["stride_time"] < THRESH["shuffle_stride_s"]:
            events.append(("SHUFFLING", f"swing {m['swing_gyro']:.0f} dps, stride {m['stride_time']:.2f} s"))
        if m["clearance_mm"] is not None and m["clearance_mm"] < THRESH["drag_clearance_mm"]:
            events.append(("FOOT_DRAG", f"clearance {m['clearance_mm']:.0f} mm"))
        if m["heel_fore_ratio"] is not None:
            if m["heel_fore_ratio"] < THRESH["heel_ratio_low"]:
                events.append(("REDUCED_HEEL_STRIKE", f"heel/forefoot {m['heel_fore_ratio']:.2f}"))
        elif m["heel_impact"] is not None and m["heel_impact"] < THRESH["piezo_heel_low"]:
            events.append(("REDUCED_HEEL_STRIKE", f"heel impact {m['heel_impact']:.0f}"))
        if m["stride_cv"] is not None and m["stride_cv"] > THRESH["stride_cv_high"]:
            events.append(("HIGH_VARIABILITY", f"stride CV {m['stride_cv']:.1f}%"))
    if abs(m["turn_deg"]) > THRESH["turn_deg"]:
        events.append(("TURN", f"{m['turn_deg']:.0f} deg in 2 s"))
    return m, events


# Events that trigger an automatic biofeedback cue.
CUE_EVENTS = {"FREEZING": {"n": 6, "ms": 250}, "SHUFFLING": {"n": 3, "ms": 150}, "FOOT_DRAG": {"n": 2, "ms": 150}}
