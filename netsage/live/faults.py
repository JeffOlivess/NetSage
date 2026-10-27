"""Real, self-scoped fault injection for the live demo.

Every fault affects THIS PC only and is undone automatically: after its timer,
on "Clear", when NetSage exits, and at the next start-up.

  loss        firewall rule toggled at random -> real dropped packets   (admin)
  outage      block public IPs, gateway stays reachable -> no internet  (admin)
  dns         block port 53 -> name resolution fails                    (admin)
  disconnect  netsh wlan disconnect, kept down for the duration
  adapter     disable the Wi-Fi adapter                                 (admin)
  congestion  4 parallel downloads saturate the link (uses data!)
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.request

from .. import config
from . import winnet as w

RULE_PREFIX = "NetSage-Fault"
PUBLIC_RANGES = ("0.0.0.0-9.255.255.255,11.0.0.0-126.255.255.255,"
                 "128.0.0.0-169.253.255.255,169.255.0.0-172.15.255.255,"
                 "172.32.0.0-192.167.255.255,192.169.0.0-223.255.255.255")
NON_LOOPBACK = "0.0.0.0-126.255.255.255,128.0.0.0-223.255.255.255"
CONGESTION_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
CONGESTION_CAP_MB = 150

LIVE_FAULTS = {
    "loss":       {"label": "Packet loss (40%)",  "admin": True,  "expect": "PACKET_LOSS"},
    "outage":     {"label": "Internet outage",    "admin": True,  "expect": "UPSTREAM_OUTAGE"},
    "dns":        {"label": "DNS failure",        "admin": True,  "expect": "DNS_FAILURE"},
    "disconnect": {"label": "Wi-Fi disconnect",   "admin": False, "expect": "LINK_DOWN"},
    "adapter":    {"label": "Adapter disabled",   "admin": True,  "expect": "LINK_DOWN"},
    "congestion": {"label": "Congestion (uses ~150 MB data)", "admin": False, "expect": "CONGESTION"},
}
RULES = {"loss": ["Loss"], "outage": ["Outage"], "dns": ["DNS-UDP", "DNS-TCP"]}


def remove_rules(kinds=None):
    for k in (kinds or RULES):
        for suffix in RULES.get(k, []):
            w.firewall(f'delete rule name="{RULE_PREFIX}-{suffix}"')


class FaultInjector:
    def __init__(self, log):
        self.log = log
        self.lock = threading.RLock()
        self.active = None
        self._stop = threading.Event()
        self._threads = []
        self.bulk_running = False

    def status(self):
        with self.lock:
            if not self.active:
                return None
            a = {k: v for k, v in self.active.items() if not k.startswith("_")}
        a["remaining"] = max(0, round(a["until"] - time.time()))
        return a

    def inject(self, kind, seconds, iface):
        if kind not in LIVE_FAULTS:
            return {"ok": False, "error": "unknown fault"}
        if not w.IS_WINDOWS:
            return {"ok": False, "error": "Live faults need Windows."}
        if LIVE_FAULTS[kind]["admin"] and not w.is_admin():
            return {"ok": False, "error": "This fault needs Administrator rights - start NetSage "
                                          "with Start-NetSage.bat and accept the admin prompt."}
        seconds = int(max(10, min(300, seconds or 60)))
        self.clear("replaced")
        name = (iface or {}).get("name") or "Wi-Fi"
        profile = (iface or {}).get("profile") or (iface or {}).get("ssid")
        info = {"kind": kind, "label": LIVE_FAULTS[kind]["label"], "started": time.time(),
                "until": time.time() + seconds, "seconds": seconds, "interface": name,
                "profile": profile, "ssid": (iface or {}).get("ssid")}
        self._stop = threading.Event()
        ok, err = True, ""
        if kind == "loss":
            ok, err = w.firewall(f'add rule name="{RULE_PREFIX}-Loss" dir=out action=block '
                                 f'protocol=any remoteip={NON_LOOPBACK} enable=no')
            if ok:
                self._spawn(self._loss_loop, 0.4)
        elif kind == "outage":
            ok, err = w.firewall(f'add rule name="{RULE_PREFIX}-Outage" dir=out action=block '
                                 f'protocol=any remoteip={PUBLIC_RANGES}')
        elif kind == "dns":
            ok, err = w.firewall(f'add rule name="{RULE_PREFIX}-DNS-UDP" dir=out action=block '
                                 'protocol=udp remoteport=53')
            if ok:
                ok, err = w.firewall(f'add rule name="{RULE_PREFIX}-DNS-TCP" dir=out action=block '
                                     'protocol=tcp remoteport=53')
        elif kind == "disconnect":
            if not profile:
                return {"ok": False, "error": "Not connected to Wi-Fi."}
            r = w.disconnect(name); ok, err = r["ok"], r["output"]
            if ok:
                self._spawn(self._keep_down, name)
        elif kind == "adapter":
            r = w.set_interface(name, False); ok, err = r["ok"], r["output"]
        elif kind == "congestion":
            info["downloaded_mb"] = 0.0
            self.bulk_running = True
            for _ in range(4):
                self._spawn(self._download)
        if not ok:
            remove_rules()
            return {"ok": False, "error": f"Windows refused: {err or 'unknown error'}"}
        with self.lock:
            self.active = info
        self._save()
        self._spawn(self._timer)
        self.log("fault", f"Injected live fault: {info['label']} for {seconds}s")
        return {"ok": True}

    def clear(self, reason="manual"):
        with self.lock:
            a, self.active = self.active, None
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []
        self.bulk_running = False
        if not a:
            return {"ok": True}
        remove_rules()
        if a["kind"] == "adapter":
            w.set_interface(a["interface"], True); time.sleep(2)
            if a.get("profile"):
                w.reconnect(a["profile"], a.get("ssid"), a["interface"])
        elif a["kind"] == "disconnect" and a.get("profile"):
            w.reconnect(a["profile"], a.get("ssid"), a["interface"])
        self._save()
        if reason != "replaced":
            self.log("info", f"Fault ended: {a['label']} ({reason})")
        return {"ok": True}

    # -- partial undo used by self-healing actions -----------------------------
    def stop_bulk(self):
        with self.lock:
            a = self.active
        if a and a["kind"] == "congestion":
            self._stop.set(); self.bulk_running = False
            return True
        return False

    def stop_keepdown(self):
        with self.lock:
            a = self.active
        if a and a["kind"] == "disconnect":
            self._stop.set()
            return True
        return False

    def startup_cleanup(self):
        remove_rules()
        try:
            st = json.loads(config.FAULT_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = None
        if st and st.get("kind") == "adapter":
            w.set_interface(st.get("interface") or "Wi-Fi", True)
        if st and st.get("kind") in ("adapter", "disconnect") and st.get("profile"):
            w.reconnect(st["profile"], st.get("ssid"), st.get("interface"))
        try:
            config.FAULT_STATE_PATH.unlink()
        except OSError:
            pass

    # -- internals ---------------------------------------------------------------
    def _spawn(self, fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        self._threads.append(t); t.start()

    def _timer(self):
        while not self._stop.wait(0.5):
            with self.lock:
                a = self.active
            if not a:
                return
            if time.time() >= a["until"]:
                threading.Thread(target=self.clear, kwargs={"reason": "timer"}, daemon=True).start()
                return
        # stop event set by a healing action: keep the timer for bookkeeping only
        while True:
            with self.lock:
                a = self.active
            if not a:
                return
            if time.time() >= a["until"]:
                threading.Thread(target=self.clear, kwargs={"reason": "timer"}, daemon=True).start()
                return
            time.sleep(0.5)

    def _loss_loop(self, p):
        on = False
        while not self._stop.is_set():
            want = random.random() < p
            if want != on:
                w.firewall(f'set rule name="{RULE_PREFIX}-Loss" new enable={"yes" if want else "no"}')
                on = want
            self._stop.wait(random.uniform(0.4, 0.9))
        w.firewall(f'set rule name="{RULE_PREFIX}-Loss" new enable=no')

    def _keep_down(self, name):
        while not self._stop.wait(2):
            cur = w.current_interface()
            if cur and cur.get("state") == "connected":
                w.disconnect(name)

    def _download(self):
        while not self._stop.is_set():
            with self.lock:
                a = self.active
                if a and a.get("downloaded_mb", 0) >= CONGESTION_CAP_MB:
                    self.bulk_running = False
                    return
            try:
                req = urllib.request.Request(CONGESTION_URL, headers={"User-Agent": "NetSage"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    while not self._stop.is_set():
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        with self.lock:
                            if self.active:
                                self.active["downloaded_mb"] = round(
                                    self.active.get("downloaded_mb", 0) + len(chunk) / 1e6, 1)
            except Exception:  # noqa: BLE001
                self._stop.wait(1)

    def _save(self):
        try:
            with self.lock:
                a = self.active
            config.FAULT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            if a:
                config.FAULT_STATE_PATH.write_text(json.dumps(
                    {k: a.get(k) for k in ("kind", "interface", "profile", "ssid")}), encoding="utf-8")
            elif config.FAULT_STATE_PATH.exists():
                config.FAULT_STATE_PATH.unlink()
        except OSError:
            pass
