"""Optional ML classifier run on every analysis window.

Train a scikit-learn model on the features in FEATURES (from the public datasets, then your own
labelled sessions), save it with joblib.dump(model, "model.joblib") in this folder, and restart
the app. Without a model file, predict() returns (None, None) and only the rule-based events run.
"""
from pathlib import Path

FEATURES = ["cadence", "stride_time", "stride_cv", "stance_pct", "heel_fore_ratio",
            "heel_impact", "swing_gyro", "clearance_mm", "freeze_index"]
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
    """Returns (label, confidence) for one window of metrics, or (None, None)."""
    _load()
    if _model is None or not metrics.get("walking") and metrics.get("freeze_index") is None:
        return None, None
    x = [[0.0 if metrics.get(f) is None else float(metrics[f]) for f in FEATURES]]  # missing -> 0
    if hasattr(_model, "predict_proba"):
        p = _model.predict_proba(x)[0]
        i = int(p.argmax())
        return str(_model.classes_[i]), float(p[i])
    return str(_model.predict(x)[0]), None
