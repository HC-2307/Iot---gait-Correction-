"""Train, validate and test the freeze-of-gait model on FoG-STAR, then save fog_model.joblib.

  python train_fog.py                        # reads ../Datasets/fogstar/sensor_data.csv
  python train_fog.py path/to/sensor_data.csv

Each ankle is used as a separate one-foot example. Recordings are cut into windows (step 0.5 s);
a window counts as a freeze when more than half of it is labelled FoG. Nothing is ever split
inside a subject: both ankles and every window of a person stay on the same side.

  1. Split   6 of the 22 subjects are set aside as the test set by a fixed rule (split()).
  2. Validate  on the other 16, leave-one-subject-out: pick the window length, then the model.
  3. Test    the chosen model, trained on the 16, is scored once on the 6 it has never seen.
  4. Extra   leave-one-subject-out over all 22, and the 2 s-ahead prediction proof of concept.
  5. Save    the chosen model retrained on all 22 subjects, for the live service.

The printed report is also written to fog_model_report.txt.
"""
import sys
from pathlib import Path

import joblib
import numpy as np
from joblib import Parallel, delayed
from sklearn.base import clone
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, roc_curve
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

import fog_model

FS = fog_model.FS
STEP_S = 0.5
PRE_S = 2.0                                   # seconds before a freeze labelled pre-FoG (Shalin et al. 2021)
WINDOWS = [(2, 2), (3, 3), (4, 4), (2, 6), (3, 6), (4, 8)]   # (window s, context s) tried in step 2
DATA = Path(__file__).resolve().parent.parent / "Datasets" / "fogstar" / "sensor_data.csv"
REPORT = Path(__file__).with_name("fog_model_report.txt")
ANKLES = {"L": 1, "R": 7}                     # first of each ankle's six columns (acc xyz, gyro xyz)
ACTIVITY, FOG, SEVERITY, SUBJECT = 25, 26, 27, 28
ACTIVITIES = {1: "walk", 2: "sit", 3: "stand", 6: "turn right", 7: "turn left"}
SEVERITIES = {1: "shuffling", 2: "trembling", 3: "akinesia"}

MODELS = {
    "Logistic regression": make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced", max_iter=2000)),
    "Random forest": RandomForestClassifier(300, min_samples_leaf=5, class_weight="balanced_subsample", random_state=0),
    "Extra trees": ExtraTreesClassifier(300, min_samples_leaf=5, class_weight="balanced_subsample", random_state=0),
    "Gradient boosting": HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                                        l2_regularization=1.0, class_weight="balanced", random_state=0),
    "SVM (RBF)": make_pipeline(StandardScaler(), SVC(class_weight="balanced", probability=True, random_state=0)),
    "Neural net (MLP)": make_pipeline(StandardScaler(), MLPClassifier((32, 16), alpha=0.01, early_stopping=True,
                                                                      max_iter=300, random_state=0)),
}

_lines = []


def log(text=""):
    print(text, flush=True)
    _lines.append(text)


def load(path):
    """Returns the data and the (start, stop) rows of each recording."""
    d = np.loadtxt(path, delimiter=",", skiprows=1)
    cut = np.flatnonzero((np.diff(d[:, SUBJECT:SUBJECT + 3], axis=0) != 0).any(axis=1)) + 1
    bounds = np.r_[0, cut, len(d)]
    return d, list(zip(bounds[:-1], bounds[1:]))


def episodes(d, recs):
    """Freeze episodes as rows of (recording, onset s, offset s), timed from the recording's start."""
    out = []
    for r, (a, b) in enumerate(recs):
        edge = np.diff(np.r_[0, d[a:b, FOG], 0])
        out += [(r, on / FS, off / FS) for on, off in zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1))]
    return np.array(out)


def split(d):
    """Test set: every 4th freezer when ranked by time frozen, plus 2 of the 6 non-freezers.
    Fixed before any model was trained, so the test subjects cover heavy and light freezers."""
    subs = np.unique(d[:, SUBJECT])
    frozen = np.array([d[d[:, SUBJECT] == s, FOG].sum() for s in subs])
    freezers = subs[frozen > 0][np.argsort(-frozen[frozen > 0], kind="stable")]
    test = np.r_[freezers[1::4], subs[frozen == 0][1::3]]
    return np.setdiff1d(subs, test), np.sort(test), freezers


def build(d, recs, win_s, ctx_s):
    """Cuts every recording into windows, once per ankle. Returns (X, m); m has one row per window."""
    w, c, step = int(win_s * FS), int(ctx_s * FS), int(STEP_S * FS)
    X, m = [], []
    for r, (a, b) in enumerate(recs):
        fog = d[a:b, FOG]
        target = fog.copy()  # prediction target: a freeze, or the PRE_S before one
        edge = np.diff(np.r_[0, fog, 0])
        for on, off in zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1)):
            target[max(0, on - min(int(PRE_S * FS), off - on)):on] = 1
        for ankle, first in enumerate(ANKLES.values()):
            sig = d[a:b, first:first + 6]
            gaps = np.r_[0, np.cumsum(np.isnan(sig).any(axis=1))]
            for e in range(w, b - a + 1, step):
                s = max(0, e - c)
                if gaps[e] - gaps[s]:
                    continue  # sensor dropout inside the window
                X.append(fog_model.features(sig[s:e, :3], sig[s:e, 3:], win_s, ctx_s))
                m.append((d[a, SUBJECT], r, ankle, e / FS, fog[e - w:e].mean() > 0.5, target[e - 1],
                          d[a + e - 1, ACTIVITY], d[a + e - w:a + e, SEVERITY].max()))
    names = ["subject", "rec", "ankle", "t", "y", "target", "activity", "severity"]
    return np.array(X), np.rec.fromrecords(m, names=names)


def oof(model, X, y, groups):
    """Out-of-fold freeze probabilities, leaving one subject out at a time."""
    def fold(s):
        te = groups == s
        return te, clone(model).fit(X[~te], y[~te]).predict_proba(X[te])[:, 1]
    p = np.zeros(len(y))
    for te, pr in Parallel(n_jobs=-1)(delayed(fold)(s) for s in np.unique(groups)):
        p[te] = pr
    return p


def youden(y, p):
    """Threshold with the largest sensitivity + specificity."""
    fpr, tpr, thr = roc_curve(y, p)
    return float(thr[np.argmax(tpr - fpr)])


def scores(y, p, thr):
    y, pred = np.asarray(y, bool), p >= thr
    tp, fn, tn, fp = (y & pred).sum(), (y & ~pred).sum(), (~y & ~pred).sum(), (~y & pred).sum()
    sens, spec, prec = tp / (tp + fn), tn / (tn + fp), tp / max(tp + fp, 1)
    return {"auc": roc_auc_score(y, p), "sens": sens, "spec": spec, "acc": (tp + tn) / len(y),
            "bal_acc": (sens + spec) / 2, "prec": prec, "f1": 2 * prec * sens / max(prec + sens, 1e-9)}


def subject_aucs(m, p, y):
    """AUC inside each subject that has both freeze and non-freeze windows."""
    return np.array([roc_auc_score(y[m.subject == s], p[m.subject == s])
                     for s in np.unique(m.subject) if 0 < y[m.subject == s].mean() < 1])


def fmt(s):
    return (f"AUC {s['auc']:.3f}  sens {s['sens']:.1%}  spec {s['spec']:.1%}  accuracy {s['acc']:.1%}  "
            f"balanced acc {s['bal_acc']:.1%}  precision {s['prec']:.1%}  F1 {s['f1']:.3f}")


def episode_report(m, p, thr, eps, lo, hi):
    """Per freeze episode and ankle: was there an alarm between onset+lo and onset+hi (hi None = offset)?
    Returns (episodes checked, share with an alarm, seconds from onset to the first alarm)."""
    first = []
    for r, on, off in eps:
        for ankle in (0, 1):
            k = (m.rec == r) & (m.ankle == ankle) & (m.t > on + lo) & (m.t <= (off if hi is None else on + hi))
            if k.any():
                hit = m.t[k][p[k] >= thr]
                first.append(hit.min() - on if len(hit) else np.nan)
    first = np.array(first)
    return len(first), np.mean(~np.isnan(first)), first[~np.isnan(first)]


def breakdown(m, p, thr):
    for code, name in SEVERITIES.items():
        k = m.y & (m.severity == code)
        if k.any():
            log(f"    {name:<10} windows caught: {(p[k] >= thr).mean():.1%} of {k.sum()}")
    for code, name in ACTIVITIES.items():
        k = ~m.y & (m.activity == code)
        if k.any():
            log(f"    false alarms while {name:<10}: {(p[k] >= thr).mean():.1%} of {k.sum()} windows")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DATA
    d, recs = load(path)
    eps = episodes(d, recs)
    dev, test, freezers = split(d)
    rec_subject = np.array([d[a, SUBJECT] for a, _ in recs])
    log(f"FoG-STAR: {len(d)} rows, {len(recs)} recordings, {len(eps)} freeze episodes, "
        f"{d[:, FOG].mean():.1%} of rows frozen")
    log(f"Validation subjects ({len(dev)}): {dev.astype(int).tolist()}")
    log(f"Test subjects ({len(test)}): {test.astype(int).tolist()}")

    # ---- validation 1: window length (two reference models, 16 validation subjects) ----
    log("\n== Validation 1: window length (leave-one-subject-out AUC on the validation subjects) ==")
    data, mean_auc = {}, {}
    for win_s, ctx_s in WINDOWS:
        X, m = build(d, recs, win_s, ctx_s)
        data[(win_s, ctx_s)] = (X, m)
        k = np.isin(m.subject, dev)
        aucs = [roc_auc_score(m.y[k], oof(MODELS[n], X[k], m.y[k], m.subject[k]))
                for n in ("Logistic regression", "Gradient boosting")]
        log(f"  window {win_s} s, context {ctx_s} s: logistic {aucs[0]:.3f}, boosting {aucs[1]:.3f}, "
            f"mean {np.mean(aucs):.3f}  ({k.sum()} windows, {m.y[k].mean():.1%} freeze)")
        mean_auc[(win_s, ctx_s)] = np.mean(aucs)
    # Settings within 0.005 of the best are a tie at this sample size: take the shortest window (fastest alarm).
    close = [c for c in WINDOWS if mean_auc[c] > max(mean_auc.values()) - 0.005]
    win_s, ctx_s = min(close, key=lambda c: (c[0], -mean_auc[c]))
    X, m = data[(win_s, ctx_s)]
    k = np.isin(m.subject, dev)
    Xd, md = X[k], m[k]
    log(f"  chosen: window {win_s} s, context {ctx_s} s, {X.shape[1]} features")

    # ---- validation 2: model ----
    log("\n== Validation 2: model (leave-one-subject-out on the validation subjects) ==")
    fi = Xd[:, fog_model.feature_names(win_s, ctx_s).index("acc_win_fi")]
    log(f"  {'Freeze Index alone (baseline)':<30} AUC {roc_auc_score(md.y, fi):.3f}")
    oofs = {}
    for name, model in MODELS.items():
        p = oofs[name] = oof(model, Xd, md.y, md.subject)
        s, per = scores(md.y, p, youden(md.y, p)), subject_aucs(md, p, md.y)
        log(f"  {name:<30} AUC {s['auc']:.3f}  per-subject AUC median {np.median(per):.3f} "
            f"(min {per.min():.3f})  sens {s['sens']:.1%}  spec {s['spec']:.1%}")
    name = max(oofs, key=lambda n: roc_auc_score(md.y, oofs[n]))
    thr = youden(md.y, oofs[name])
    log(f"  chosen: {name}, alarm threshold {thr:.2f} (largest sensitivity + specificity on validation)")

    # ---- test: trained on the validation subjects, scored once on the held-out subjects ----
    log(f"\n== Test: {name}, trained on {len(dev)} subjects, scored on the {len(test)} held out ==")
    Xt, mt = X[~k], m[~k]
    pt = clone(MODELS[name]).fit(Xd, md.y).predict_proba(Xt)[:, 1]
    test_scores = scores(mt.y, pt, thr)
    log(f"  {len(mt)} windows, {mt.y.mean():.1%} freeze")
    log("  " + fmt(test_scores))
    te_eps = eps[np.isin(rec_subject[eps[:, 0].astype(int)], test)]
    n, share, delay = episode_report(mt, pt, thr, te_eps, 0, None)
    log(f"  freeze episodes with an alarm while they last: {share:.1%} of {n} (episode x ankle); "
        f"median {np.median(delay):.1f} s from onset to alarm")
    breakdown(mt, pt, thr)

    # ---- extra 1: every subject left out in turn ----
    log(f"\n== Leave-one-subject-out over all 22 subjects: {name} ==")
    pa = oof(MODELS[name], X, m.y, m.subject)
    all_scores, per = scores(m.y, pa, thr), subject_aucs(m, pa, m.y)
    log("  " + fmt(all_scores))
    log(f"  per-subject AUC: median {np.median(per):.3f}, min {per.min():.3f}, max {per.max():.3f} "
        f"({len(per)} subjects with freezes)")
    n, share, delay = episode_report(m, pa, thr, eps, 0, None)
    log(f"  freeze episodes with an alarm while they last: {share:.1%} of {n}; median {np.median(delay):.1f} s to alarm")
    breakdown(m, pa, thr)
    log("  other alarm thresholds (set THRESH['fog_prob'] in analysis.py to use one):")
    for t in (0.1, 0.2, 0.3, 0.5):
        s, walk = scores(m.y, pa, t), ~m.y & (m.activity == 1)
        log(f"    {t:.2f}: sens {s['sens']:.1%}  spec {s['spec']:.1%}  precision {s['prec']:.1%}  "
            f"false alarms while walking {(pa[walk] >= t).mean():.1%}")

    # ---- extra 2: prediction proof of concept (pre-FoG + FoG against the rest, freezers only) ----
    log(f"\n== Prediction proof of concept: {name}, target = freeze or the {PRE_S:.0f} s before it ==")
    f = np.isin(m.subject, freezers)
    Xf, mf = X[f], m[f]
    target = mf.target == 1
    pp = oof(MODELS[name], Xf, target, mf.subject)
    pthr = youden(target, pp)
    pred_scores = scores(target, pp, pthr)
    log(f"  leave-one-subject-out over the {len(freezers)} freezers, threshold {pthr:.2f}")
    log("  " + fmt(pred_scores))
    n, share, lead = episode_report(mf, pp, pthr, eps, -PRE_S, 0)
    log(f"  episodes with an alarm in the {PRE_S:.0f} s before onset: {share:.1%} of {n}; "
        f"median warning {np.median(-lead):.1f} s")
    log(f"  for comparison, alarms in windows that are neither freeze nor pre-freeze: {1 - pred_scores['spec']:.1%}")

    # ---- save: the chosen model, retrained on every subject ----
    final = clone(MODELS[name]).fit(X, m.y)
    joblib.dump({"model": final, "name": name, "win_s": win_s, "ctx_s": ctx_s, "threshold": thr,
                 "features": fog_model.feature_names(win_s, ctx_s),
                 "scores": {"test": test_scores, "loso_all": all_scores, "prediction": pred_scores}},
                fog_model.MODEL_PATH, compress=3)
    log(f"\nSaved {fog_model.MODEL_PATH.name} ({name}, retrained on all 22 subjects; "
        f"{fog_model.MODEL_PATH.stat().st_size / 1e6:.1f} MB)")
    REPORT.write_text("\n".join(_lines) + "\n")


if __name__ == "__main__":
    main()
