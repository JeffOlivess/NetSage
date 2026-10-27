"""Export the exact datasets the two networks were trained on, as CSV files.

    python scripts/export_dataset.py

dataset/diagnosis_dataset.csv   24,000 labelled samples (12 features + fault class),
                                with the train/test split and the model's prediction
dataset/timeseries_dataset.csv  telemetry time series used for fault prediction
dataset/README.txt              column descriptions
"""
import csv
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netsage.dataset import sequence, static_samples        # noqa: E402
from netsage.features import CLASS_CODES, FEATURE_KEYS, transform  # noqa: E402
from netsage.models import Brain                             # noqa: E402

OUT = ROOT / "dataset"
OUT.mkdir(exist_ok=True)

# Same generator, seeds and split as models.train_all()
X, y = static_samples(n_per_class=3000)
idx = np.random.default_rng(3).permutation(len(X))
split = np.empty(len(X), dtype=object)
split[idx[:int(0.8 * len(idx))]] = "train"
split[idx[int(0.8 * len(idx)):]] = "test"
pred = Brain().diag.predict_proba(transform(X))
with open(OUT / "diagnosis_dataset.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["sample_id"] + FEATURE_KEYS + ["fault_class", "split", "model_prediction", "model_confidence"])
    for i in range(len(X)):
        w.writerow([i + 1] + [round(float(v), 2) for v in X[i]] + [CLASS_CODES[y[i]], split[i],
                   CLASS_CODES[int(pred[i].argmax())], round(float(pred[i].max()), 4)])

rng = np.random.default_rng(11)          # same seed as the predictor's training data
H = 15
with open(OUT / "timeseries_dataset.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["sequence_id", "t"] + FEATURE_KEYS + ["fault_active", "fault_within_15"])
    for s in range(300):
        seq, act = sequence(rng)
        for t in range(len(seq)):
            w.writerow([s + 1, t] + [round(float(v), 2) for v in seq[t]] +
                       [int(act[t]), int(act[t:t + H].any())])
print("written to", OUT)
