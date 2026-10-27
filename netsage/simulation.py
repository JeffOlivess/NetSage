"""The simulated production network ("plant") used in Simulation mode.

It runs the analytical network model with hidden fault dynamics (gradual
build-ups and abrupt failures) and measurement noise, and produces the same
telemetry feature vector as the live Wi-Fi monitor.
"""
from __future__ import annotations

import copy

import numpy as np

from . import playbooks
from .features import FEATURE_KEYS, TIMEOUT_MS, MAX_RATIO
from .netmodel import baseline_state, evaluate, lid

SIM_FAULTS = {
    "link_errors":  {"label": "Fibre errors on CORE-1 to ACC-1", "kind": "gradual", "ramp": 12,
                     "expect": "PACKET_LOSS"},
    "congestion":   {"label": "Cloud backup saturates ISP-A", "kind": "gradual", "ramp": 25,
                     "expect": "CONGESTION"},
    "cpu_storm":    {"label": "Broadcast storm overloads CORE-1", "kind": "gradual", "ramp": 22,
                     "expect": "DEVICE_OVERLOAD"},
    "interference": {"label": "Radio interference on AP-1", "kind": "gradual", "ramp": 18,
                     "expect": "WEAK_SIGNAL"},
    "isp_outage":   {"label": "ISP-A provider outage", "kind": "abrupt", "ramp": 1,
                     "expect": "UPSTREAM_OUTAGE"},
    "dns_crash":    {"label": "DNS-1 service crash", "kind": "abrupt", "ramp": 1,
                     "expect": "DNS_FAILURE"},
    "ap_failure":   {"label": "AP-1 radio failure", "kind": "abrupt", "ramp": 1,
                     "expect": "LINK_DOWN"},
}

class SimNetwork:
    def __init__(self, seed=None):
        self.rng = np.random.default_rng(seed)
        self.reset()

    def reset(self):
        self.state = baseline_state()
        self.faults = {}          # kind -> ticks since injection
        self.t = 0
        self.base_latency = evaluate(self.state)["probe"]["inet_latency"]
        self._undo = {}

    # ---------------------------------------------------------------- faults
    def inject(self, kind):
        if kind not in SIM_FAULTS:
            return {"ok": False, "error": f"unknown fault {kind}"}
        self.faults[kind] = 0
        return {"ok": True, "fault": kind, "label": SIM_FAULTS[kind]["label"]}

    def repair(self):
        """A technician fixes the root causes; configuration changes stay."""
        st = self.state
        st["demand"]["backup"] = 0.0
        st["ext_interference"]["level"] = 0.0
        for nd in st["nodes"].values():
            nd["cpu_storm"] = 0.0; nd["oper_up"] = True
            if nd["kind"] == "dns":
                nd["service_up"] = True
        for L in st["links"].values():
            L["loss_err"] = 0.0; L["oper_up"] = True
        self.faults = {}

    def _advance_faults(self):
        st = self.state
        for kind in list(self.faults):
            self.faults[kind] += 1
            p = min(1.0, self.faults[kind] / SIM_FAULTS[kind]["ramp"])
            if kind == "link_errors":
                st["links"][lid("CORE-1", "ACC-1")]["loss_err"] = 18.0 * p
            elif kind == "congestion":
                st["demand"]["backup"] = 640.0 * p
            elif kind == "cpu_storm":
                st["nodes"]["CORE-1"]["cpu_storm"] = 75.0 * p
            elif kind == "interference":
                st["ext_interference"]["level"] = 0.85 * p
            elif kind == "isp_outage":
                st["links"][lid("ISP-A", "INTERNET")]["oper_up"] = False
            elif kind == "dns_crash":
                st["nodes"]["DNS-1"]["service_up"] = False
            elif kind == "ap_failure":
                st["nodes"]["AP-1"]["oper_up"] = False

    # ---------------------------------------------------------------- actions
    def apply(self, code, action_id):
        for act in playbooks.candidates(code, self.state):
            if act.id == action_id:
                self._undo[action_id] = self._config_snapshot()
                act.apply(self.state)
                if action_id == "reboot_dev":
                    self.faults.pop("cpu_storm", None)
                return {"ok": True, "title": act.title}
        return {"ok": False, "error": f"action {action_id} not available"}

    def rollback(self, action_id):
        snap = self._undo.pop(action_id, None)
        if snap is None:
            return {"ok": False}
        st = self.state
        st["policy"] = snap["policy"]; st["groups"] = snap["groups"]
        for k, v in snap["links"].items():
            st["links"][k]["cost"], st["links"][k]["admin_up"] = v
        for n, v in snap["nodes"].items():
            st["nodes"][n]["admin_up"], st["nodes"][n]["channel"], st["nodes"][n]["tx_boost"] = v
        return {"ok": True}

    def _config_snapshot(self):
        st = self.state
        return {"policy": copy.deepcopy(st["policy"]), "groups": copy.deepcopy(st["groups"]),
                "links": {k: (L["cost"], L["admin_up"]) for k, L in st["links"].items()},
                "nodes": {n: (d["admin_up"], d.get("channel"), d.get("tx_boost"))
                          for n, d in st["nodes"].items()}}

    # ---------------------------------------------------------------- telemetry
    def step(self):
        self.t += 1
        self._advance_faults()
        ev = evaluate(self.state)
        return self.telemetry(ev), ev

    def telemetry(self, ev):
        p = ev["probe"]; r = self.rng
        d = {}
        if not p["link_up"]:
            d.update(gw_latency=TIMEOUT_MS, gw_loss=100, inet_latency=TIMEOUT_MS, inet_loss=100,
                     jitter=0, dns_fail=100, signal=0, link_up=0, util=0, err_rate=0,
                     cpu=p["max_cpu"] or 20, lat_ratio=MAX_RATIO)
        else:
            def loss(pct, n=20):
                return 100.0 * r.binomial(n, min(1.0, pct / 100)) / n
            gw_l = loss(p["gw_loss"]) if p["gw_latency"] is not None else 100.0
            in_l = loss(p["inet_loss"]) if p["reach_inet"] else 100.0
            gw = p["gw_latency"] * r.lognormal(0, 0.08) if p["gw_latency"] is not None else TIMEOUT_MS
            inet = p["inet_latency"] * r.lognormal(0, 0.06) if p["reach_inet"] else TIMEOUT_MS
            if in_l >= 100:
                inet = TIMEOUT_MS
            if gw_l >= 100:
                gw = TIMEOUT_MS
            jitter = 0.06 * min(inet, 400) + 0.35 * max(0.0, min(inet, 400) - self.base_latency) \
                + r.uniform(0, 2)
            dns_ok = p["dns_ext"] and r.random() > p["inet_loss"] / 100
            err = max([ev["links"][k]["err"] for k in _path_links(p["path"])] or [0])
            d.update(gw_latency=gw, gw_loss=gw_l, inet_latency=inet, inet_loss=in_l,
                     jitter=jitter, dns_fail=0.0 if dns_ok else 100.0,
                     signal=float(np.clip(p["signal"] + r.normal(0, 1.5), 0, 100)), link_up=1,
                     util=min(100.0, p["max_util"] * r.lognormal(0, 0.03)),
                     err_rate=err * r.lognormal(0, 0.1), cpu=min(100.0, p["max_cpu"] + r.normal(0, 1)),
                     lat_ratio=MAX_RATIO if inet >= TIMEOUT_MS else min(MAX_RATIO, inet / self.base_latency))
        return [float(d[k]) for k in FEATURE_KEYS]


def _path_links(nodes):
    return [lid(a, b) for a, b in zip(nodes[:-1], nodes[1:])]
