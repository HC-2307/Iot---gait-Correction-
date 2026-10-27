"""Train the gait classifier from labelled recordings and save model.joblib.

  python train_model.py                  # every finished session in the database
  python train_model.py --skip 1,5,16,19 # leave out bad sessions (by number)
  python train_model.py a.csv b.csv      # or session CSVs exported from the dashboard

Sessions labelled "other" are never used.

Each recording is cut into 10 s windows (step 1 s), run through the same analysis.analyze() the
live service uses, and the window features (model.FEATURES) are labelled with the session label.
Windows from one recording are highly correlated, so the score reported here is optimistic;
test on recordings (or people) the model has not seen for the review numbers.
"""
import sys

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold, cross_val_predict

import analysis
import config
import model
from db import COLS, DB


args = sys.argv[1:]
SKIP = set()
if "--skip" in args:
    i = args.index("--skip")
    SKIP = {int(v) for v in args[i + 1].split(",")}
    del args[i:i + 2]


def load_recordings():
    if args:
        import csv
        for path in args:
            with open(path) as f:
                r = list(csv.DictReader(f))
            yield path, r[0]["label"], np.array([[float(x[c]) for c in COLS] for x in r])
    else:
        db = DB()
        for sid, label in db.query("SELECT id, label FROM sessions WHERE end_ts IS NOT NULL ORDER BY id"):
            if sid in SKIP or label == "other":
                continue
            data = db.query(f"SELECT {', '.join(COLS)} FROM samples WHERE session=? ORDER BY ts", (sid,))
            if data:
                yield f"session {sid}", label, np.array(data, dtype=float)


def windows(a, size=config.WINDOW_S * config.FS, step=config.FS):
    for start in range(0, len(a) - size + 1, step):
        yield a[start:start + size]


X, y, groups = [], [], []
seen = {}
for name, label, a in load_recordings():
    g = seen[label] = seen.get(label, -1) + 1  # 1st, 2nd, ... recording of this label
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

# ExtraTrees: best of 6 models when tested on a whole recording round it had not seen (2026-10-27),
# and its scores for every label are used on the dashboard.
clf = ExtraTreesClassifier(n_estimators=300, max_depth=8, class_weight="balanced", random_state=0)
# Fold k tests the (k+1)-th recording of every label, so each fold still has other recordings of every
# label to learn from and is tested only on recordings it has never seen.
k = min(seen.values()) + 1
if k < 2:
    print(f"\nSkipping cross-validation: record at least 2 sessions per label first.")
else:
    groups = [min(g, k - 1) for g in groups]
    pred = cross_val_predict(clf, X, y, groups=groups, cv=GroupKFold(n_splits=k))
    print("\nCross-validated, each recording tested by a model that never saw it:\n"
          + classification_report(y, pred, zero_division=0))
clf.fit(X, y)
joblib.dump(clf, model.MODEL_PATH)
print(f"Saved {model.MODEL_PATH} (the running app picks it up automatically)")
