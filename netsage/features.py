"""Telemetry features and fault classes shared by every part of NetSage.

The same 12-number feature vector is produced by the simulated enterprise
network and by the live Wi-Fi monitor, so one trained model serves both.
"""
import numpy as np

# key, label, unit
FEATURES = [
    ("gw_latency",   "Gateway latency",      "ms"),
    ("gw_loss",      "Gateway packet loss",  "%"),
    ("inet_latency", "Internet latency",     "ms"),
    ("inet_loss",    "Internet packet loss", "%"),
    ("jitter",       "Jitter",               "ms"),
    ("lat_ratio",    "Latency vs baseline",  "x"),
    ("dns_fail",     "DNS failure rate",     "%"),
    ("signal",       "Wi-Fi signal",         "%"),
    ("link_up",      "Link state",           ""),
    ("util",         "Link utilisation",     "%"),
    ("err_rate",     "Interface errors",     "%"),
    ("cpu",          "Device CPU",           "%"),
]
FEATURE_KEYS = [f[0] for f in FEATURES]
FEATURE_LABELS = {f[0]: f[1] for f in FEATURES}
FEATURE_UNITS = {f[0]: f[2] for f in FEATURES}
N_FEATURES = len(FEATURES)

TIMEOUT_MS = 1000.0       # an unanswered probe counts as this latency
MAX_RATIO = 20.0

# code, title, severity, one-line meaning
CLASSES = [
    ("HEALTHY",         "Healthy",            "ok",   "All paths and services respond normally."),
    ("LINK_DOWN",       "Link down",          "crit", "The access link is down; nothing is reachable."),
    ("PACKET_LOSS",     "Packet loss",        "warn", "A significant share of packets is being dropped."),
    ("CONGESTION",      "Congestion",         "warn", "A link is saturated; queues add heavy delay."),
    ("UPSTREAM_OUTAGE", "Upstream outage",    "crit", "The gateway answers but the internet does not."),
    ("DNS_FAILURE",     "DNS failure",        "warn", "IP connectivity works but name resolution fails."),
    ("WEAK_SIGNAL",     "Wireless degraded",  "warn", "Poor radio conditions cause loss and delay."),
    ("DEVICE_OVERLOAD", "Device overload",    "warn", "A network device is CPU-bound and forwarding slowly."),
]
CLASS_CODES = [c[0] for c in CLASSES]
CLASS_INFO = {c[0]: {"title": c[1], "severity": c[2], "meaning": c[3]} for c in CLASSES}
N_CLASSES = len(CLASSES)

_LAT = {"gw_latency", "inet_latency", "jitter"}


def to_vector(d):
    """dict -> raw feature vector (missing values filled sensibly)."""
    return np.array([float(d.get(k, 0.0) or 0.0) for k in FEATURE_KEYS], dtype=float)


def transform(raw):
    """Fixed, scale-free normalisation used by both networks.

    Works on a single vector or a 2-D batch.
    """
    x = np.array(raw, dtype=float, copy=True)
    one = x.ndim == 1
    if one:
        x = x[None, :]
    for i, k in enumerate(FEATURE_KEYS):
        col = np.clip(x[:, i], 0, None)
        if k in _LAT:
            x[:, i] = np.log1p(np.minimum(col, TIMEOUT_MS)) / np.log1p(TIMEOUT_MS)
        elif k == "lat_ratio":
            x[:, i] = np.log1p(np.minimum(col, MAX_RATIO)) / np.log1p(MAX_RATIO)
        elif k == "link_up":
            x[:, i] = np.clip(col, 0, 1)
        else:
            x[:, i] = np.minimum(col, 100.0) / 100.0
    return x[0] if one else x


def trend_vector(window):
    """Predictor input: recent mean (12) + per-tick slope (12) over a window."""
    w = np.asarray(window, dtype=float)
    if w.ndim == 1:
        w = w[None, :]
    t = transform(w)
    mean = t[-3:].mean(axis=0)
    if len(t) >= 2:
        xs = np.arange(len(t)) - (len(t) - 1) / 2.0
        slope = (xs[:, None] * (t - t.mean(axis=0))).sum(axis=0) / (xs ** 2).sum()
    else:
        slope = np.zeros(t.shape[1])
    return np.concatenate([mean, slope * 10.0])


TREND_LABELS = ([FEATURE_LABELS[k] for k in FEATURE_KEYS]
                + [FEATURE_LABELS[k] + " (trend)" for k in FEATURE_KEYS])
