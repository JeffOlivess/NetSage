"""The two deep networks: fault Diagnoser and fault Predictor, plus their
explanations (integrated gradients + plain-language evidence)."""
from __future__ import annotations

import json
import time

import numpy as np

from . import config
from .dataset import static_samples, trend_samples
from .features import (CLASS_CODES, CLASS_INFO, FEATURE_KEYS, FEATURE_LABELS,
                       FEATURE_UNITS, N_CLASSES, N_FEATURES, TREND_LABELS,
                       transform, trend_vector)
from .nn import DeepNet

# A typical healthy link -- the reference point explanations are measured from.
HEALTHY_REF = {"gw_latency": 3, "gw_loss": 0, "inet_latency": 25, "inet_loss": 0,
               "jitter": 3, "lat_ratio": 1, "dns_fail": 0, "signal": 75, "link_up": 1,
               "util": 25, "err_rate": 0, "cpu": 30}
HEALTHY_VEC = np.array([HEALTHY_REF[k] for k in FEATURE_KEYS], float)


# ---------------------------------------------------------------- training
def train_all(quick=False, verbose=True):
    t0 = time.time()
    n = 800 if quick else 3000
    X, y = static_samples(n_per_class=n)
    Xt = transform(X)
    rng = np.random.default_rng(3)
    idx = rng.permutation(len(Xt)); cut = int(0.8 * len(idx))
    tr, te = idx[:cut], idx[cut:]
    diag = DeepNet([N_FEATURES, 64, 64, 32, N_CLASSES], "multiclass", seed=1)
    hist = diag.fit(Xt[tr], y[tr], epochs=12 if quick else 25, lr=3e-3,
                    X_val=Xt[te], y_val=y[te], verbose=False)
    pred = diag.predict_proba(Xt[te]).argmax(axis=1)
    cm = np.zeros((N_CLASSES, N_CLASSES), int)
    for a, b in zip(y[te], pred):
        cm[a, b] += 1
    diag.meta = {"name": "Fault Diagnoser", "classes": CLASS_CODES, "features": FEATURE_KEYS,
                 "test_accuracy": round(float((pred == y[te]).mean()), 4)}
    diag.save(config.DIAGNOSER_PATH)
    if verbose:
        print(f"Diagnoser  test accuracy {diag.meta['test_accuracy']:.4f}")

    Xp, yp = trend_samples(n_sequences=600 if quick else 2500)
    idx = rng.permutation(len(Xp)); cut = int(0.8 * len(idx))
    tr2, te2 = idx[:cut], idx[cut:]
    pos = yp[tr2].mean()
    cw = {0: 0.5 / (1 - pos), 1: 0.5 / pos}
    predn = DeepNet([2 * N_FEATURES, 64, 32, 1], "binary", seed=2)
    hist2 = predn.fit(Xp[tr2], yp[tr2], epochs=10 if quick else 20, lr=2e-3, class_weight=cw)
    pp = predn.predict_proba(Xp[te2]) >= 0.5
    tp = int((pp & (yp[te2] == 1)).sum()); fp = int((pp & (yp[te2] == 0)).sum())
    fn = int((~pp & (yp[te2] == 1)).sum())
    predn.meta = {"name": "Fault Predictor", "horizon_ticks": config.PRED_HORIZON,
                  "window_ticks": config.PRED_WINDOW,
                  "test_accuracy": round(float((pp == (yp[te2] == 1)).mean()), 4),
                  "precision": round(tp / max(1, tp + fp), 4),
                  "recall": round(tp / max(1, tp + fn), 4)}
    predn.save(config.PREDICTOR_PATH)
    if verbose:
        print(f"Predictor  accuracy {predn.meta['test_accuracy']:.4f}  "
              f"precision {predn.meta['precision']:.4f}  recall {predn.meta['recall']:.4f}")

    recall = {CLASS_CODES[i]: round(cm[i, i] / max(1, cm[i].sum()), 4) for i in range(N_CLASSES)}
    metrics = {
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
        "train_seconds": round(time.time() - t0, 1),
        "diagnoser": {"architecture": [N_FEATURES, 64, 64, 32, N_CLASSES],
                      "activation": "ReLU / softmax", "optimiser": "Adam + cosine decay, L2",
                      "params": diag.n_params(), "train_samples": int(len(tr)),
                      "test_samples": int(len(te)), "test_accuracy": diag.meta["test_accuracy"],
                      "per_class_recall": recall, "confusion": cm.tolist(),
                      "classes": CLASS_CODES, "loss_curve": [h["loss"] for h in hist],
                      "val_curve": [h.get("val_acc") for h in hist]},
        "predictor": {"architecture": [2 * N_FEATURES, 64, 32, 1],
                      "activation": "ReLU / sigmoid", "params": predn.n_params(),
                      "train_samples": int(len(tr2)), "test_samples": int(len(te2)),
                      **{k: predn.meta[k] for k in ("test_accuracy", "precision", "recall")},
                      "horizon_ticks": config.PRED_HORIZON,
                      "loss_curve": [h["loss"] for h in hist2]},
    }
    config.METRICS_PATH.write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    return metrics


# ---------------------------------------------------------------- inference
class Brain:
    """Loads both networks and turns raw telemetry into explained decisions."""

    def __init__(self):
        if not (config.DIAGNOSER_PATH.exists() and config.PREDICTOR_PATH.exists()):
            print("No trained models found - training now (about a minute)...")
            train_all(verbose=True)
        self.diag = DeepNet.load(config.DIAGNOSER_PATH)
        self.pred = DeepNet.load(config.PREDICTOR_PATH)
        try:
            self.metrics = json.loads(config.METRICS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.metrics = {}
        self._base_t = transform(HEALTHY_VEC)

    def diagnose(self, raw):
        x = transform(raw)
        p = self.diag.predict_proba(x)[0]
        i = int(p.argmax())
        code = CLASS_CODES[i]
        attr = self.diag.integrated_gradients(x, self._base_t, out_index=i)
        factors = []
        for k, a, v in zip(FEATURE_KEYS, attr, raw):
            factors.append({"key": k, "label": FEATURE_LABELS[k], "value": _fmt(k, v),
                            "contribution": round(float(a), 4)})
        factors.sort(key=lambda f: -abs(f["contribution"]))
        tot = sum(abs(f["contribution"]) for f in factors) or 1.0
        for f in factors:
            f["share"] = round(100 * abs(f["contribution"]) / tot, 1)
        return {"code": code, **CLASS_INFO[code], "confidence": round(float(p[i]), 4),
                "probabilities": {c: round(float(q), 4) for c, q in zip(CLASS_CODES, p)},
                "factors": factors[:6], "evidence": evidence(code, raw)}

    def predict(self, window):
        """window: list of raw vectors (oldest first). Returns early-warning info."""
        if len(window) < 4:
            return {"risk": 0.0, "forecast": None, "eta_s": None, "drivers": []}
        x = trend_vector(window)
        risk = float(self.pred.predict_proba(x)[0])
        attr = self.pred.integrated_gradients(
            x, np.concatenate([self._base_t, np.zeros(N_FEATURES)]), 0)
        drivers = sorted(zip(TREND_LABELS, attr), key=lambda t: -t[1])[:3]
        # Forecast what the fault will be: extrapolate the trend and diagnose it.
        w = np.asarray(window, float)
        # Theil-Sen slope (median of pairwise slopes) is robust to single spikes.
        i, j = np.triu_indices(len(w), k=1)
        slope = np.median((w[j] - w[i]) / (j - i)[:, None], axis=0)
        future = np.clip(np.median(w[-3:], axis=0) + slope * config.PRED_HORIZON, 0, None)
        ki = FEATURE_KEYS.index("link_up"); future[ki] = min(1, round(future[ki]))
        p = self.diag.predict_proba(transform(future))[0]
        order = np.argsort(-p)
        fc = CLASS_CODES[order[0]] if CLASS_CODES[order[0]] != "HEALTHY" else CLASS_CODES[order[1]]
        return {"risk": round(risk, 4), "forecast": fc, "forecast_title": CLASS_INFO[fc]["title"],
                "forecast_probs": {c: round(float(q), 4) for c, q in zip(CLASS_CODES, p)},
                "drivers": [{"label": l, "weight": round(float(a), 4)} for l, a in drivers if a > 0]}


def _fmt(k, v):
    u = FEATURE_UNITS[k]
    if k == "link_up":
        return "up" if v >= 0.5 else "down"
    if k == "lat_ratio":
        return f"{v:.1f}x"
    if u == "ms" and v >= 999:
        return "timeout"
    return f"{v:.0f}{u}" if v >= 10 or u == "%" else f"{v:.1f}{u}"


def evidence(code, raw):
    d = dict(zip(FEATURE_KEYS, raw))
    gw = "timeout" if d["gw_latency"] >= 999 else f"{d['gw_latency']:.0f} ms"
    out = []
    if code == "HEALTHY":
        out.append(f"Gateway answers in {gw}; internet loss {d['inet_loss']:.0f}%; "
                   f"DNS failures {d['dns_fail']:.0f}%.")
    elif code == "LINK_DOWN":
        out.append("No reply from the gateway or the internet."
                   + (" The interface reports the link as DOWN." if d["link_up"] < 0.5
                      else " The interface is up but no frames get through."))
    elif code == "UPSTREAM_OUTAGE":
        out.append(f"The gateway still answers ({gw}) but {d['inet_loss']:.0f}% of internet "
                   "probes are lost, so the fault lies beyond the gateway.")
    elif code == "DNS_FAILURE":
        out.append(f"{d['dns_fail']:.0f}% of DNS queries fail while ping to the internet loses "
                   f"only {d['inet_loss']:.0f}%: routing works, name resolution does not.")
    elif code == "PACKET_LOSS":
        where = "between this host and the gateway" if d["gw_loss"] >= 0.5 * d["inet_loss"] \
            else "beyond the gateway"
        out.append(f"{d['inet_loss']:.0f}% internet loss and {d['gw_loss']:.0f}% gateway loss: "
                   f"packets are dropped {where}.")
        if d["err_rate"] > 3:
            out.append(f"Interface error rate of {d['err_rate']:.0f}% points to a physical-layer fault.")
    elif code == "CONGESTION":
        out.append(f"Latency is {d['lat_ratio']:.1f}x the link's normal value with "
                   f"{d['util']:.0f}% utilisation: queues are building on a saturated link.")
    elif code == "WEAK_SIGNAL":
        out.append(f"Signal is only {d['signal']:.0f}% with {d['inet_loss']:.0f}% loss and "
                   f"{d['jitter']:.0f} ms jitter: the radio link is degraded.")
    elif code == "DEVICE_OVERLOAD":
        out.append(f"Device CPU at {d['cpu']:.0f}% while latency is {d['lat_ratio']:.1f}x normal: "
                   "packets are delayed by an overloaded device, not by link capacity.")
    return out
