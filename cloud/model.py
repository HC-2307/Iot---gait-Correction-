"""Optional ML classifier run on every analysis window.

Train a scikit-learn model on the features in FEATURES (from the public datasets, then your own
labelled sessions), save it with joblib.dump(model, "model.joblib") in this folder, and restart
the app. Without a model file, predict() returns (None, None) and only the rule-based events run.
"""
from pathlib import Path

# clearance_mm, heel_impact and stance_pct are left out: the ToF was dead in most of round 1 (so clearance
# marks the session, not the gait), heel_impact is always 4095 and stance_pct always 100 when walking.
FEATURES = ["cadence", "stride_time", "stride_cv", "heel_fore_ratio", "swing_gyro", "freeze_index",
            "acc_std", "gyro_rms", "rotation_deg",
            "p1_peak", "p1_hits_per_step", "p1_iqr", "p1_sat", "p2_peak", "p2_hits_per_step", "p2_iqr", "p2_sat",
            "roll_lag_s", "p1_p2_corr", "f1_range", "f2_range",
            "gx_std", "ax_std", "ax_p95", "gy_std", "ay_std", "ay_p95", "gz_std", "az_std", "az_p95",
            "acc_peak", "swing_peak"]
MODEL_PATH = Path(__file__).with_name("model.joblib")

_model = None
_loaded_mtime = None


def _load():
    global _model, _loaded_mtime
    if not MODEL_PATH.exists():
        _model = None
        return
    mtime = MODEL_PATH.stat().st_mtime
    if mtime != _loaded_mtime:  # reload when the file is replaced
        import joblib
        _model = joblib.load(MODEL_PATH)
        _loaded_mtime = mtime
        print(f"[model] loaded {MODEL_PATH.name}: {getattr(_model, 'classes_', '?')}")


def predict(metrics: dict):
    """Returns (label, confidence, {label: score for every label}) for one window, or (None, None, None)."""
    _load()
    if _model is None or not metrics.get("walking") and metrics.get("freeze_index") is None:
        return None, None, None
    x = [[0.0 if metrics.get(f) is None else float(metrics[f]) for f in FEATURES]]  # missing -> 0
    if hasattr(_model, "predict_proba"):
        p = _model.predict_proba(x)[0]
        i = int(p.argmax())
        return str(_model.classes_[i]), float(p[i]), {str(c): round(float(v), 3) for c, v in zip(_model.classes_, p)}
    return str(_model.predict(x)[0]), None, None
