"""Synthetic training data with realistic fault signatures.

Two generators:
  * static samples  -> one feature vector per fault class (diagnoser)
  * time series     -> healthy periods, gradual fault onsets and abrupt
                       failures (predictor: "will a fault start soon?")

Baselines are randomised (2 ms campus LAN up to 120 ms phone hotspot) so the
same model works on the simulated enterprise network and on a real link.
"""
from __future__ import annotations

import numpy as np

from .features import CLASS_CODES, FEATURE_KEYS, TIMEOUT_MS, MAX_RATIO

GRADUAL = ["CONGESTION", "DEVICE_OVERLOAD", "WEAK_SIGNAL", "PACKET_LOSS"]
ABRUPT = ["LINK_DOWN", "UPSTREAM_OUTAGE", "DNS_FAILURE"]


def _healthy(rng):
    base = float(np.exp(rng.uniform(np.log(3), np.log(120))))      # internet baseline ms
    gw = float(np.exp(rng.uniform(np.log(0.8), np.log(max(1.0, min(base * 0.6, 40))))))
    loss = min(4.0, rng.exponential(0.4)) if rng.random() < 0.5 else 0.0
    return {
        "_base": base,
        "gw_latency": gw * rng.uniform(0.8, 1.3),
        "gw_loss": min(loss, rng.exponential(0.3)),
        "inet_latency": base * rng.uniform(0.85, 1.35),
        "inet_loss": loss,
        "jitter": base * rng.uniform(0.03, 0.3) + rng.uniform(0, 3),
        "dns_fail": 0.0 if rng.random() < 0.8 else rng.uniform(0, 15),
        "signal": rng.uniform(28, 100),
        "link_up": 1.0,
        "util": rng.uniform(0, 72),
        "err_rate": rng.uniform(0, 0.5),
        "cpu": rng.uniform(3, 85),
    }


def _fault(cls, h, rng):
    f = dict(h)
    base = h["_base"]
    if cls == "LINK_DOWN":
        f.update(gw_latency=TIMEOUT_MS, gw_loss=100, inet_latency=TIMEOUT_MS, inet_loss=100,
                 jitter=0, dns_fail=100, util=0, err_rate=rng.uniform(0, 5))
        if rng.random() < 0.75:
            f.update(link_up=0, signal=0)
        else:   # associated, but no frames get through
            f.update(signal=rng.uniform(10, 90))
    elif cls == "PACKET_LOSS":
        L = rng.uniform(4, 70)
        f.update(inet_loss=L, gw_loss=L * rng.uniform(0.0, 1.15),
                 inet_latency=base * rng.uniform(1.0, 2.2),
                 jitter=h["jitter"] * rng.uniform(1.2, 4) + rng.uniform(2, 15),
                 dns_fail=min(100, L * rng.uniform(0.4, 1.4)),
                 err_rate=rng.uniform(0, 25) if rng.random() < 0.6 else rng.uniform(0, 1),
                 signal=rng.uniform(32, 100))
    elif cls == "CONGESTION":
        r = rng.uniform(2.2, 15)
        f.update(inet_latency=min(TIMEOUT_MS * 0.9, base * r),
                 gw_latency=h["gw_latency"] * rng.uniform(1, r),
                 jitter=base * r * rng.uniform(0.15, 0.6) + rng.uniform(10, 40),
                 inet_loss=rng.uniform(0, 8), gw_loss=rng.uniform(0, 4),
                 util=rng.uniform(62, 100), dns_fail=rng.uniform(0, 25),
                 cpu=rng.uniform(10, 75))
    elif cls == "UPSTREAM_OUTAGE":
        f.update(inet_latency=TIMEOUT_MS, inet_loss=rng.uniform(90, 100),
                 jitter=rng.uniform(0, 5), dns_fail=rng.uniform(30, 100),
                 util=rng.uniform(0, 20))
    elif cls == "DNS_FAILURE":
        f.update(dns_fail=rng.uniform(40, 100))
    elif cls == "WEAK_SIGNAL":
        L = rng.uniform(2, 30)
        f.update(signal=rng.uniform(5, 38), inet_loss=L, gw_loss=L * rng.uniform(0.6, 1.2),
                 inet_latency=base * rng.uniform(1.2, 4),
                 gw_latency=h["gw_latency"] * rng.uniform(1.5, 6),
                 jitter=h["jitter"] * rng.uniform(2, 6) + rng.uniform(5, 20),
                 dns_fail=min(100, L * rng.uniform(0.3, 1.5)), util=rng.uniform(0, 40))
    elif cls == "DEVICE_OVERLOAD":
        f.update(cpu=rng.uniform(80, 100), inet_latency=base * rng.uniform(1.5, 5),
                 gw_latency=h["gw_latency"] * rng.uniform(1.5, 8),
                 jitter=h["jitter"] * rng.uniform(1.5, 5) + rng.uniform(3, 20),
                 inet_loss=rng.uniform(0, 8), gw_loss=rng.uniform(0, 6),
                 util=rng.uniform(20, 70))
    return f


def _finish(d):
    d = dict(d)
    base = d.pop("_base")
    d["lat_ratio"] = min(MAX_RATIO, d["inet_latency"] / max(base, 1.0))
    if d["inet_loss"] >= 99:
        d["lat_ratio"] = MAX_RATIO
    return [float(d[k]) for k in FEATURE_KEYS]


def static_samples(n_per_class=3000, seed=7):
    rng = np.random.default_rng(seed)
    X, y = [], []
    for ci, cls in enumerate(CLASS_CODES):
        for _ in range(n_per_class):
            h = _healthy(rng)
            d = h if cls == "HEALTHY" else _fault(cls, h, rng)
            X.append(_finish(_noisy(d, rng))); y.append(ci)
    return np.array(X), np.array(y)


def _mix(h, f, p):
    out = {}
    for k in h:
        if k == "_base":
            out[k] = h[k]
        elif k in ("gw_latency", "inet_latency", "jitter"):
            out[k] = float(np.exp(np.log(h[k] + 1) * (1 - p) + np.log(f[k] + 1) * p) - 1)
        else:
            out[k] = h[k] + (f[k] - h[k]) * p
    return out


def _noisy(d, rng):
    d = dict(d)
    for k in ("gw_latency", "inet_latency", "jitter"):
        d[k] = max(0.3, d[k] * rng.lognormal(0, 0.12))
    for k in ("gw_loss", "inet_loss", "dns_fail", "util", "cpu", "err_rate"):
        d[k] = float(np.clip(d[k] + rng.normal(0, 1.2), 0, 100))
    d["signal"] = float(np.clip(d["signal"] + rng.normal(0, 2), 0, 100))
    return d


def sequence(rng, length=90):
    """One telemetry time series + per-tick 'fault active' flags."""
    h = _healthy(rng)
    seq, active = [], []
    kind = rng.choice(["none", "gradual", "gradual", "abrupt", "blip"])
    t0 = int(rng.integers(20, length - 25))
    if kind == "gradual":
        cls = str(rng.choice(GRADUAL)); f = _fault(cls, h, rng); ramp = int(rng.integers(10, 30))
    elif kind == "abrupt":
        cls = str(rng.choice(ABRUPT)); f = _fault(cls, h, rng); ramp = 1
    else:
        cls, f, ramp = None, None, 1
    for t in range(length):
        if f is not None and t >= t0:
            p = min(1.0, (t - t0 + 1) / ramp)
            d = _mix(h, f, p); act = p >= 0.7
        elif kind == "blip" and t0 <= t < t0 + 2:     # short benign spike
            d = dict(h); d["inet_latency"] *= rng.uniform(1.5, 3); d["jitter"] *= 2; act = False
        else:
            d = h; act = False
        seq.append(_finish(_noisy(d, rng))); active.append(act)
    return np.array(seq), np.array(active)


def trend_samples(n_sequences=2500, window=10, horizon=15, seed=11):
    from .features import trend_vector
    rng = np.random.default_rng(seed)
    X, y = [], []
    for _ in range(n_sequences):
        seq, act = sequence(rng)
        for t in range(window, len(seq), 2):
            X.append(trend_vector(seq[t - window:t]))
            y.append(int(act[t - 1:t - 1 + horizon].any()))
    return np.array(X), np.array(y)
