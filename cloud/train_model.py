"""Train the gait classifier from labelled recordings and save model.joblib.

  python train_model.py                  # every finished session in the database
  python train_model.py a.csv b.csv      # or session CSVs exported from the dashboard

Each recording is cut into 10 s windows (step 1 s), run through the same analysis.analyze() the
live service uses, and the window features (model.FEATURES) are labelled with the session label.
Windows from one recording are highly correlated, so the score reported here is optimistic;
test on recordings (or people) the model has not seen for the review numbers.
"""
import sys

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold, cross_val_predict

import analysis
import config
import model
from db import COLS, DB


def load_recordings():
    if len(sys.argv) > 1:
        import csv
        for path in sys.argv[1:]:
            with open(path) as f:
                r = list(csv.DictReader(f))
            yield path, r[0]["label"], np.array([[float(x[c]) for c in COLS] for x in r])
    else:
        db = DB()
        for sid, label in db.query("SELECT id, label FROM sessions WHERE end_ts IS NOT NULL"):
            data = db.query(f"SELECT {', '.join(COLS)} FROM samples WHERE session=? ORDER BY ts", (sid,))
            if data:
                yield f"session {sid}", label, np.array(data, dtype=float)


def windows(a, size=config.WINDOW_S * config.FS, step=config.FS):
    for start in range(0, len(a) - size + 1, step):
        yield a[start:start + size]


X, y, groups = [], [], []
for g, (name, label, a) in enumerate(load_recordings()):
    n = 0
    for w in windows(a):
        m, _ = analysis.analyze(w)
        X.append([0.0 if m.get(f) is None else float(m[f]) for f in model.FEATURES])
        y.append(label)
        groups.append(g)
        n += 1
    print(f"{name}: {label}, {n} windows")

if len(set(y)) < 2:
    sys.exit("Need recordings of at least two different labels.")

clf = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=0)
rec_per_label = {lab: len({g for g, l in zip(groups, y) if l == lab}) for lab in set(y)}
k = min(5, len(set(groups)))
if min(rec_per_label.values()) < 2:
    print(f"\nSkipping cross-validation: record at least 2 sessions per label first {rec_per_label}.")
else:
    pred = cross_val_predict(clf, X, y, groups=groups, cv=GroupKFold(n_splits=k))
    print("\nCross-validated by recording:\n" + classification_report(y, pred, zero_division=0))
clf.fit(X, y)
joblib.dump(clf, model.MODEL_PATH)
print(f"Saved {model.MODEL_PATH} (the running app picks it up automatically)")
