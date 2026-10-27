"""Per-access-point scorecard for one SSID (e.g. a campus network with many APs).

Packet loss can only be measured on the AP you are connected to, so the
scorecard learns as you roam: signal from scans, loss/latency from your own
probes, plus operator marks (GOOD always preferred, BAD never auto-selected).
"""
from __future__ import annotations

import json
import time


class APScorecard:
    def __init__(self, path):
        self.path = path
        self.aps = {}
        try:
            self.aps = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.aps = {}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.aps, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _get(self, bssid):
        return self.aps.setdefault(bssid.lower(), {"mark": "none", "signal": None, "band": None,
                                                    "channel": None, "loss": None, "latency": None,
                                                    "samples": 0, "seen": 0})

    def observe_scan(self, ap):
        r = self._get(ap["bssid"])
        r.update(signal=ap.get("signal"), band=ap.get("band"), channel=ap.get("channel"),
                 ssid=ap.get("ssid"), seen=time.time())

    def observe_link(self, bssid, loss, latency):
        r = self._get(bssid)
        a = 0.3
        r["loss"] = loss if r["loss"] is None else (1 - a) * r["loss"] + a * loss
        if latency is not None:
            r["latency"] = latency if r["latency"] is None else (1 - a) * r["latency"] + a * latency
        r["samples"] += 1

    def mark(self, bssid, mark):
        self._get(bssid)["mark"] = mark
        self.save()

    def quality(self, bssid):
        r = self.aps.get(bssid.lower())
        if not r:
            return 0.0
        q = r["signal"] or 0
        if r["loss"] is not None:
            q -= min(60, r["loss"] * 3)
        if r["latency"] is not None:
            q -= min(20, max(0, r["latency"] - 50) / 10)
        if r["mark"] == "good":
            q += 100
        return round(q, 1)

    def ranked(self, visible=None):
        rows = []
        for b, r in self.aps.items():
            if visible is not None and b not in visible:
                continue
            rows.append({"bssid": b, **r, "quality": self.quality(b)})
        rows.sort(key=lambda x: -x["quality"])
        return rows

    def best(self, visible=None, exclude=()):
        for r in self.ranked(visible):
            if r["mark"] != "bad" and r["bssid"] not in exclude:
                return r
        return None
