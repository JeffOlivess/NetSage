"""Live telemetry collector: turns real probes into NetSage's feature vector.

Each tick (about 1.5 s): Wi-Fi state from netsh, one ping to the default
gateway, one ping to an internet host, one real DNS query, NIC throughput and
host CPU. Rolling windows turn these into the 12 features the deep networks
expect. Every ~20 s a background scan refreshes the access-point scorecard.
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..features import FEATURE_KEYS, MAX_RATIO, TIMEOUT_MS
from . import winnet as w
from .apcard import APScorecard

WINDOW = 2          # ticks; each tick sends 3 internet + 2 gateway probes
N_NET, N_GW = 3, 2


class LiveMonitor:
    def __init__(self, log, target="8.8.8.8"):
        self.log = log
        self.target = target
        self.rows = deque(maxlen=40)
        self.iface = None
        self.gateway = None
        self.dns = []
        self.gw_ever_ok = False
        self.baseline = None
        self.peak_mbps = 5.0
        self._last_bytes = None
        self._last_meta = 0
        self._last_scan = 0
        self._scanning = False
        self.visible = {}
        self.adapter_on = True
        self.scorecard = APScorecard(config.AP_SCORECARD_PATH)
        self.pool = ThreadPoolExecutor(max_workers=8)
        w.host_load()

    # ---------------------------------------------------------------- probes
    def tick(self, fault_active=False):
        now = time.time()
        iface = w.current_interface()
        connected = bool(iface and iface.get("state") == "connected")
        if not connected:
            name = (iface or self.iface or {}).get("name") or "Wi-Fi"
            self.adapter_on = w.adapter_enabled(name) is not False
        else:
            self.adapter_on = True
        if connected and (now - self._last_meta > 15 or not self.gateway
                          or (self.iface or {}).get("bssid") != iface.get("bssid")):
            if (self.iface or {}).get("bssid") and iface.get("bssid") != (self.iface or {}).get("bssid"):
                self.log("info", f"Now on access point {iface.get('bssid')} ({iface.get('ssid')})")
                self.gw_ever_ok = False
            self.gateway = w.default_gateway()
            self.dns = w.dns_servers(iface.get("name")) or ([self.gateway] if self.gateway else [])
            self._last_meta = now
        if iface:
            self.iface = iface
        elif self.iface:
            self.iface = dict(self.iface, state="disconnected")

        gw, net, dns = [], [], None
        if connected:
            fg = [self.pool.submit(w.ping_once, self.gateway, 800) for _ in range(N_GW)]
            fn = [self.pool.submit(w.ping_once, self.target, 1000) for _ in range(N_NET)]
            fd = self.pool.submit(w.dns_query, self.dns[0] if self.dns else None)
            gw, net, dns = [f.result() for f in fg], [f.result() for f in fn], fd.result()
        if any(x is not None for x in gw):
            self.gw_ever_ok = True

        mbps = None
        b = w.nic_bytes(iface.get("name")) if iface else None
        if b is not None and self._last_bytes:
            db, dt = b - self._last_bytes[0], now - self._last_bytes[1]
            if dt > 0 and db >= 0:
                mbps = db * 8 / dt / 1e6
                self.peak_mbps = max(self.peak_mbps, mbps)
        self._last_bytes = (b, now) if b is not None else None

        row = {"t": now, "connected": connected, "gw": gw, "net": net,
               "dns_ok": (dns is not None) if connected else None, "mbps": mbps,
               "signal": iface.get("signal") if (iface and connected) else 0, "cpu": w.host_load()}
        self.rows.append(row)
        if connected and iface.get("bssid"):
            probes = [x for r in list(self.rows)[-4:] if r["connected"] for x in r["net"]]
            loss = 100 * sum(x is None for x in probes) / max(1, len(probes))
            lat = [x for x in probes if x is not None]
            self.scorecard.observe_link(iface["bssid"], loss, statistics.mean(lat) if lat else None)
        if not fault_active:
            self._update_baseline()
        if now - self._last_scan > 20 and not self._scanning:
            self._scanning = True
            threading.Thread(target=self._scan, daemon=True).start()
        return self.features()

    def _scan(self):
        try:
            ssid = (self.iface or {}).get("ssid") or None
            aps = w.scan(ssid) if ssid else []
            vis = {}
            for ap in aps:
                self.scorecard.observe_scan(ap); vis[ap["bssid"]] = ap
            self.visible = vis
            self.scorecard.save()
        finally:
            self._last_scan = time.time()
            self._scanning = False

    def rescan(self):
        self._last_scan = 0

    def _update_baseline(self):
        lat = [x for r in list(self.rows)[-20:] for x in r["net"] if x is not None]
        if len(lat) >= 5:
            med = statistics.median(lat)
            self.baseline = med if self.baseline is None else min(self.baseline * 1.02,
                                                                  0.9 * self.baseline + 0.1 * med)

    # ---------------------------------------------------------------- features
    def features(self):
        rows = list(self.rows)[-WINDOW:]
        last = rows[-1]
        if not last["connected"]:
            d = dict(gw_latency=TIMEOUT_MS, gw_loss=100, inet_latency=TIMEOUT_MS, inet_loss=100,
                     jitter=0, lat_ratio=MAX_RATIO, dns_fail=100, signal=0, link_up=0, util=0,
                     err_rate=0, cpu=last["cpu"])
            return [float(d[k]) for k in FEATURE_KEYS]
        con = [r for r in rows if r["connected"]]
        net_all = [x for r in con for x in r["net"]]
        gw_all = [x for r in con for x in r["gw"]]
        net = [x for x in net_all if x is not None]
        inet_loss = 100 * sum(x is None for x in net_all) / max(1, len(net_all))
        if self.gw_ever_ok:
            gws = [x for x in gw_all if x is not None]
            gw_loss = 100 * sum(x is None for x in gw_all) / max(1, len(gw_all))
            gw_lat = statistics.mean(gws) if gws else TIMEOUT_MS
        else:   # gateway ignores ping: fall back to the internet path
            gw_loss = inet_loss
            gw_lat = (min(net) * 0.3) if net else TIMEOUT_MS
        inet_lat = statistics.mean(net) if net else TIMEOUT_MS
        dnsr = [r["dns_ok"] for r in list(self.rows)[-3:] if r["dns_ok"] is not None]
        base = self.baseline or (inet_lat if net else 50.0)
        tp = [r["mbps"] for r in rows if r["mbps"] is not None]
        d = dict(gw_latency=gw_lat, gw_loss=gw_loss, inet_latency=inet_lat, inet_loss=inet_loss,
                 jitter=statistics.pstdev(net) if len(net) > 1 else 0.0,
                 lat_ratio=MAX_RATIO if not net else min(MAX_RATIO, inet_lat / max(base, 1)),
                 dns_fail=100 * sum(not x for x in dnsr) / len(dnsr) if dnsr else 0.0,
                 signal=last["signal"] or 0, link_up=1,
                 util=min(100.0, 100 * statistics.mean(tp[-3:]) / self.peak_mbps) if tp else 0.0,
                 err_rate=0.0, cpu=last["cpu"])
        return [float(d[k]) for k in FEATURE_KEYS]

    def context(self):
        it = self.iface or {}
        return {"connected": it.get("state") == "connected", "ssid": it.get("ssid"),
                "profile": it.get("profile"), "bssid": it.get("bssid"), "name": it.get("name"),
                "signal": it.get("signal"), "band": it.get("band"), "channel": it.get("channel"),
                "gateway": self.gateway, "dns": self.dns, "target": self.target,
                "adapter_on": self.adapter_on, "gw_pingable": self.gw_ever_ok,
                "baseline": round(self.baseline, 1) if self.baseline else None,
                "visible": list(self.visible.values())}
