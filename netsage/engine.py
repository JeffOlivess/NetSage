"""Runtime engine: one background loop that collects telemetry (simulated
enterprise network or live Wi-Fi), runs the deep networks, keeps the digital
twin in sync and drives the self-healing loop."""
from __future__ import annotations

import atexit
import threading
import time
from collections import deque

import numpy as np

from . import PROJECT_TITLE, __version__, config
from .features import CLASS_INFO, FEATURE_KEYS, FEATURE_LABELS, FEATURE_UNITS
from .healer import Healer
from .models import Brain
from .netmodel import PROBE_GROUP, lid
from .simulation import SIM_FAULTS, SimNetwork
from .twin import DigitalTwin


class SimEnv:
    def __init__(self, plant, twin):
        self.plant, self.twin = plant, twin

    def evaluate(self, code, projected=False):
        return self.twin.evaluate_candidates(code, projected)

    def execute(self, code, aid):
        return self.plant.apply(code, aid)

    def rollback(self, aid):
        return self.plant.rollback(aid)


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.brain = Brain()
        self.events = deque(maxlen=150)
        self.mode = "sim"
        self.running = True
        self.live = None
        self._init_sim()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- setup
    def _init_sim(self):
        self.plant = SimNetwork()
        self.twin = DigitalTwin(self.plant.state)
        self.env = SimEnv(self.plant, self.twin)
        self._fresh_healer()
        self.log("info", "Simulation mode: enterprise campus network (15 devices, 260 users)")

    def _fresh_healer(self):
        old = getattr(self, "healer", None)
        self.healer = Healer(self.env, self.log, verify_ticks=14 if self.mode == "live" else None)
        if old:
            self.healer.auto, self.healer.predictive = old.auto, old.predictive
        self.raw = deque(maxlen=30)
        self.history = deque(maxlen=180)
        self.diag = None
        self._last_raw = None
        self.pred = {"risk": 0.0, "forecast": None, "drivers": []}
        self.t0 = time.time()
        self.fault_t = None

    def _init_live(self):
        from .live.faults import FaultInjector
        from .live.monitor import LiveMonitor
        from .live.twin import LiveEnv, LiveTwin
        from .live import winnet
        if self.live is None:
            inj = FaultInjector(self.log)
            if winnet.IS_WINDOWS:
                inj.startup_cleanup()
                atexit.register(lambda: inj.clear("NetSage exit"))
                _console_close_handler(inj)
            mon = LiveMonitor(self.log)
            self.live = {"inj": inj, "mon": mon, "twin": LiveTwin(mon, inj)}
        self.env = LiveEnv(self.live["twin"], self.log)
        self._fresh_healer()
        self.log("info", "Live mode: monitoring this PC's real Wi-Fi connection")

    def set_mode(self, mode):
        with self.lock:
            if mode == self.mode:
                return {"ok": True}
            if self.mode == "live" and self.live:
                self.live["inj"].clear("mode change")
            self.mode = mode
            if mode == "live":
                self._init_live()
            else:
                self._init_sim()
        return {"ok": True}

    def log(self, kind, text):
        self.events.appendleft({"t": time.time(), "kind": kind, "text": text})

    # ---------------------------------------------------------------- loop
    def _loop(self):
        while True:
            t0 = time.time()
            try:
                if self.running:
                    with self.lock:
                        self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must never die
                self.log("error", f"engine error: {e.__class__.__name__}: {e}")
            period = config.SIM_TICK_S if self.mode == "sim" else config.LIVE_TICK_S
            time.sleep(max(0.1, period - (time.time() - t0)))

    def tick(self):
        if self.mode == "sim":
            x, ev = self.plant.step()
            self.twin.sync(self.plant.state, {"inet_latency": x[FEATURE_KEYS.index("inet_latency")]})
        else:
            inj = self.live["inj"]
            x = self.live["mon"].tick(fault_active=inj.status() is not None)
            self.live["twin"].sync(dict(zip(FEATURE_KEYS, x)))
            self._ap_guard()
        self.raw.append(x)
        if self.mode == "sim":
            # median of 3 samples: robust to a single transitional sample
            cur = np.median(np.array(list(self.raw)[-config.DIAG_WINDOW:]), axis=0)
        else:
            # live probes are already windowed by the monitor
            cur = np.mean(np.array(list(self.raw)[-2:]), axis=0)
        raw_diag = self.brain.diagnose(cur)
        self.pred = self.brain.predict(list(self.raw)[-config.PRED_WINDOW:])
        prev_code = self.diag["code"] if self.diag else "HEALTHY"
        # Displayed diagnosis changes only when the new class holds for 2 samples.
        if self.diag is None or raw_diag["code"] == prev_code or raw_diag["code"] == self._last_raw:
            self.diag = raw_diag
        self._last_raw = raw_diag["code"]
        if self.diag["code"] != prev_code and len(self.raw) > 3:
            if self.diag["code"] == "HEALTHY":
                self.log("info", "Diagnosis: healthy")
            else:
                extra = ""
                if self.fault_t:
                    extra = f" - {time.time() - self.fault_t:.1f} s after injection"
                    self.fault_t = None
                self.log("diagnose", f"Diagnosis: {self.diag['title']} "
                                     f"({self.diag['confidence']*100:.0f}%){extra}")
        self.healer.tick(raw_diag, self.pred)
        self.history.append({"t": round(time.time() - self.t0, 1),
                             **{k: round(float(v), 2) for k, v in zip(FEATURE_KEYS, x)},
                             "code": self.diag["code"], "risk": self.pred["risk"]})

    def _ap_guard(self):
        """Live AP policy: if this PC sits on an AP marked BAD, steer to the best one."""
        tw = self.live["twin"]
        s = tw.state
        if not (self.healer.auto and s and s["connected"] and s["current_mark"] == "bad"):
            return
        if self.healer.phase == "VERIFYING":
            return
        now = time.time()
        if now - getattr(self, "_guard_t", 0) < 20:
            return
        self._guard_t = now
        if s["best"] and s["profile"]:
            from .live import winnet
            r = winnet.connect_bssid(s["profile"], s["best"]["bssid"])
            self.log("heal", f"AP policy: {s['bssid']} is marked BAD - steering to "
                             f"{s['best']['bssid']}" + ("" if r.get("ok") else f" (failed: {r.get('error')})"))
        else:
            self.log("escalate", f"AP policy: {s['bssid']} is marked BAD but no better AP is visible")

    # ---------------------------------------------------------------- commands
    def inject(self, kind, seconds=60):
        with self.lock:
            if self.mode == "sim":
                r = self.plant.inject(kind)
                if r["ok"]:
                    self.log("fault", f"Injected: {r['label']}")
            else:
                r = self.live["inj"].inject(kind, seconds, self.live["mon"].iface)
            if r.get("ok"):
                self.fault_t = time.time()
            return r

    def clear(self):
        with self.lock:
            if self.mode == "sim":
                self.plant.reset()
                self.twin = DigitalTwin(self.plant.state)
                self.env = SimEnv(self.plant, self.twin)
                self._fresh_healer()
                self.log("info", "Network reset: faults repaired and configuration restored")
            else:
                self.live["inj"].clear("manual")
                self.healer.reset()
            return {"ok": True}

    def settings(self, auto=None, predictive=None):
        with self.lock:
            if auto is not None:
                self.healer.auto = bool(auto)
            if predictive is not None:
                self.healer.predictive = bool(predictive)
            self.log("info", f"Auto-heal {'on' if self.healer.auto else 'off'}, "
                             f"predictive healing {'on' if self.healer.predictive else 'off'}")
            return {"ok": True}

    def approve(self, action_id=None):
        with self.lock:
            return self.healer.approve(action_id)

    def whatif(self, code):
        with self.lock:
            if code not in CLASS_INFO:
                return {"ok": False, "error": "unknown class"}
            if self.mode == "live" and not self.live["twin"].state:
                return {"ok": False, "error": "twin not ready"}
            return {"ok": True, "code": code, "title": CLASS_INFO[code]["title"],
                    **self.env.evaluate(code, False)}

    def ap_mark(self, bssid, mark):
        if not self.live:
            return {"ok": False, "error": "live mode not started"}
        self.live["mon"].scorecard.mark(bssid, mark)
        self.log("info", f"Access point {bssid} marked {mark.upper()}")
        return {"ok": True}

    def ap_connect(self, bssid):
        from .live import winnet
        s = self.live["twin"].state if self.live else None
        if not s or not s["profile"]:
            return {"ok": False, "error": "not connected to a saved Wi-Fi network"}
        if self.live["mon"].scorecard.aps.get(bssid, {}).get("mark") == "bad":
            return {"ok": False, "error": "that access point is marked BAD"}
        r = winnet.connect_bssid(s["profile"], bssid)
        self.log("info", f"Manual connect to {bssid}: {'requested' if r.get('ok') else r.get('error')}")
        return r

    # ---------------------------------------------------------------- snapshot
    def snapshot(self):
        with self.lock:
            snap = {
                "title": PROJECT_TITLE, "version": __version__, "mode": self.mode,
                "time": time.time(), "uptime": round(time.time() - self.t0),
                "diagnosis": self.diag, "prediction": self.pred,
                "healer": self.healer.snapshot(),
                "history": list(self.history)[-120:], "events": list(self.events)[:60],
                "features": [{"key": k, "label": FEATURE_LABELS[k], "unit": FEATURE_UNITS[k]}
                             for k in FEATURE_KEYS],
                "classes": {c: v["title"] for c, v in CLASS_INFO.items()},
            }
            if self.mode == "sim":
                snap["sim"] = self._sim_view()
                snap["faults"] = [{"id": k, "label": v["label"], "kind": v["kind"],
                                   "active": k in self.plant.faults} for k, v in SIM_FAULTS.items()]
            else:
                snap["live"] = self._live_view()
            return snap

    def _sim_view(self):
        st, ev = self.twin.state, self.twin.last_eval
        probe = ev["probe"]
        path = set(lid(a, b) for a, b in zip(probe["path"][:-1], probe["path"][1:]))
        nodes = []
        for n, d in st["nodes"].items():
            e = ev["nodes"][n]
            status = "ok"
            if not e["up"]:
                status = "down"
            elif d["kind"] == "dns" and not d.get("service_up", True):
                status = "down"
            elif e["cpu"] > config.MAX_CPU:
                status = "warn"
            elif d["kind"] == "ap" and (ev["aps"][n]["interference"] >= 0.3 or ev["aps"][n]["load"] > 100):
                status = "warn"
            nodes.append({"id": n, "label": d["label"], "kind": d["kind"], "x": d["x"], "y": d["y"],
                          "status": status, "cpu": e["cpu"],
                          "extra": (f"ch {d['channel']} · {ev['aps'][n]['users']} users"
                                    if d["kind"] == "ap" else "")})
        links = []
        for k, L in st["links"].items():
            e = ev["links"][k]
            status = "down" if not e["up"] else ("warn" if e["util"] > config.MAX_LINK_UTIL or e["err"] > 1
                                                 else "ok")
            links.append({"id": k, "a": L["a"], "b": L["b"], "util": e["util"], "status": status,
                          "probe": k in path, "standby": L["cost"] >= 50})
        pol = st["policy"]
        return {"nodes": nodes, "links": links, "probe_group": PROBE_GROUP,
                "fidelity": round(self.twin.fidelity, 1) if self.twin.fidelity else None,
                "syncs": self.twin.syncs,
                "policy": {"Default route": {"A": "ISP-A", "B": "ISP-B", "share": "ISP-A + ISP-B"}[pol["isp_mode"]],
                           "QoS bulk cap": f"{pol['qos_bulk_cap']} Mb/s" if pol["qos_bulk_cap"] else "off",
                           "Client DNS": "8.8.8.8 (public)" if pol["dns_server"] == "PUBLIC" else pol["dns_server"],
                           "Control-plane policing": "on" if pol["copp"] else "off"},
                "groups": [{"name": g, "users": v["users"], "ap": v["ap"],
                            "latency": None if v["inet_latency"] is None else round(v["inet_latency"]),
                            "loss": round(v["inet_loss"], 1), "ok": v["reach_inet"] and v["dns_ext"]}
                           for g, v in ev["groups"].items()]}

    def _live_view(self):
        from .live import winnet
        from .live.faults import LIVE_FAULTS
        mon, inj, tw = self.live["mon"], self.live["inj"], self.live["twin"]
        ctx = mon.context()
        vis = {a["bssid"] for a in ctx["visible"]}
        aps = mon.scorecard.ranked(visible=None)
        for a in aps:
            a["visible"] = a["bssid"] in vis
            a["current"] = a["bssid"] == ctx.get("bssid")
        aps = [a for a in aps if a.get("ssid") == ctx.get("ssid") or a["current"]] or aps
        return {"windows": winnet.IS_WINDOWS, "admin": winnet.is_admin(),
                "location_blocked": winnet.location_blocked(),
                "location_hint": winnet.LOCATION_HINT if winnet.location_blocked() else None,
                "context": ctx, "fault": inj.status(),
                "faults": [{"id": k, "label": v["label"], "admin": v["admin"]} for k, v in LIVE_FAULTS.items()],
                "aps": aps[:12], "rules": tw.rules, "twin": tw.state and {
                    k: tw.state[k] for k in ("adapter_on", "connected", "rules", "bulk",
                                             "upstream_down", "dns_server_down", "current_mark")}}

    def model_info(self):
        return self.brain.metrics


def _console_close_handler(inj):
    try:
        import ctypes
        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _h(_evt):
            try:
                inj.clear("window closed")
            except Exception:  # noqa: BLE001
                pass
            return False
        inj._ctrl = HANDLER(_h)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(inj._ctrl, True)
    except Exception:  # noqa: BLE001
        pass
